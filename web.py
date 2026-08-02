import json
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

import database as db
import edadeal
from config import TG_BOT_TOKEN

BASE = Path(__file__).parent
app = FastAPI(title="Edadeal Bot Dashboard")

_log_buffer = []
_log_lock = threading.Lock()


def add_log(user_id, action, detail=""):
    with _log_lock:
        _log_buffer.insert(0, {
            "time": datetime.now().strftime("%d.%m %H:%M:%S"),
            "user_id": user_id,
            "action": action,
            "detail": detail,
        })
        if len(_log_buffer) > 200:
            _log_buffer[:] = _log_buffer[:200]


def _last_active_sql():
    return """(
        SELECT MAX(ts) FROM (
            SELECT a.last_spin AS ts FROM accounts a WHERE a.user_id=u.telegram_id AND a.last_spin IS NOT NULL
            UNION ALL
            SELECT p.spun_at FROM prizes p JOIN accounts a ON p.account_id=a.id WHERE a.user_id=u.telegram_id
        )
    )"""


@app.get("/api/stats")
def api_stats():
    conn = db.get_conn()
    users = conn.execute("SELECT COUNT(*) AS v FROM users").fetchone()["v"]
    total_accs = conn.execute("SELECT COUNT(*) AS v FROM accounts").fetchone()["v"]
    active_accs = conn.execute("SELECT COUNT(*) AS v FROM accounts WHERE status='active'").fetchone()["v"]
    expired_accs = conn.execute("SELECT COUNT(*) AS v FROM accounts WHERE status='expired'").fetchone()["v"]
    active_users_rows = conn.execute("SELECT DISTINCT user_id AS v FROM accounts WHERE status='active'").fetchall()
    active_users = len(active_users_rows)
    total_balance = conn.execute("SELECT COALESCE(SUM(last_balance),0) AS v FROM accounts WHERE status='active'").fetchone()["v"]
    total_spins = conn.execute("SELECT COUNT(*) AS v FROM prizes").fetchone()["v"]
    total_bonuses = conn.execute("SELECT COUNT(*) AS v FROM prizes WHERE prize_title!=''").fetchone()["v"]
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_users = conn.execute("SELECT COUNT(*) AS v FROM users WHERE created_at >= ?", (today_str,)).fetchone()["v"]
    today_accs = conn.execute("SELECT COUNT(*) AS v FROM accounts WHERE created_at >= ?", (today_str,)).fetchone()["v"]
    today_checkins = conn.execute(
        "SELECT COUNT(*) AS v FROM prizes WHERE spun_at >= ? AND prize_title!=''", (today_str,)
    ).fetchone()["v"]
    return {
        "users": users,
        "total_accounts": total_accs,
        "active_accounts": active_accs,
        "expired_accounts": expired_accs,
        "active_users": active_users,
        "total_balance": total_balance,
        "total_spins": total_spins,
        "total_bonuses": total_bonuses,
        "today_users": today_users,
        "today_accounts": today_accs,
        "today_checkins": today_checkins,
    }


@app.get("/api/stats/timeline")
def api_stats_timeline(days: int = 30):
    conn = db.get_conn()
    dates = []
    today = datetime.now().date()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        dates.append(d.strftime("%Y-%m-%d"))
    users_data = []
    accs_data = []
    active_data = []
    for d in dates:
        nxt = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        u = conn.execute("SELECT COUNT(*) AS v FROM users WHERE created_at >= ? AND created_at < ?",
                         (d, nxt)).fetchone()["v"]
        a = conn.execute("SELECT COUNT(*) AS v FROM accounts WHERE created_at >= ? AND created_at < ?",
                         (d, nxt)).fetchone()["v"]
        act = conn.execute(
            "SELECT COUNT(DISTINCT a.id) AS v FROM accounts a JOIN prizes p ON p.account_id=a.id WHERE p.spun_at >= ? AND p.spun_at < ?",
            (d, nxt)
        ).fetchone()["v"]
        users_data.append(u)
        accs_data.append(a)
        active_data.append(act)
    return {"labels": dates, "users": users_data, "accounts": accs_data, "active": active_data}


@app.get("/api/stats/daily")
def api_stats_daily(days: int = 14):
    conn = db.get_conn()
    dates = []
    today = datetime.now().date()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        dates.append(d.strftime("%Y-%m-%d"))
    spins_data = []
    checkins_data = []
    for d in dates:
        nxt = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        s = conn.execute("SELECT COUNT(*) AS v FROM prizes WHERE spun_at >= ? AND spun_at < ?",
                         (d, nxt)).fetchone()["v"]
        c = conn.execute("SELECT COUNT(*) AS v FROM prizes WHERE spun_at >= ? AND spun_at < ? AND prize_title!=''",
                         (d, nxt)).fetchone()["v"]
        spins_data.append(s)
        checkins_data.append(c)
    return {"labels": dates, "spins": spins_data, "checkins": checkins_data}


@app.get("/api/stats/balances")
def api_stats_balances():
    conn = db.get_conn()
    buckets = {"0": 0, "1-1000": 0, "1001-5000": 0, "5001-10000": 0, "10001+": 0}
    rows = conn.execute("SELECT last_balance FROM accounts WHERE status='active'").fetchall()
    for r in rows:
        b = r["last_balance"] or 0
        if b == 0:
            buckets["0"] += 1
        elif b <= 1000:
            buckets["1-1000"] += 1
        elif b <= 5000:
            buckets["1001-5000"] += 1
        elif b <= 10000:
            buckets["5001-10000"] += 1
        else:
            buckets["10001+"] += 1
    return {"labels": list(buckets.keys()), "values": list(buckets.values())}


@app.get("/api/stats/bonuses")
def api_stats_bonuses():
    conn = db.get_conn()
    rows = conn.execute("SELECT prize_title FROM prizes WHERE prize_title!=''").fetchall()
    counts = {}
    for r in rows:
        t = r["prize_title"][:40]
        counts[t] = counts.get(t, 0) + 1
    sorted_items = sorted(counts.items(), key=lambda x: -x[1])[:10]
    return {"labels": [x[0] for x in sorted_items], "values": [x[1] for x in sorted_items]}


@app.get("/api/stats/retention")
def api_stats_retention(days: int = 30):
    conn = db.get_conn()
    dates = []
    today = datetime.now().date()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        dates.append(d.strftime("%Y-%m-%d"))
    created = []
    active_now = []
    for d in dates:
        c = conn.execute("SELECT COUNT(*) AS v FROM accounts WHERE created_at <= ?", (d,)).fetchone()["v"]
        a = conn.execute(
            "SELECT COUNT(*) AS v FROM accounts WHERE status='active' AND created_at <= ?", (d,)
        ).fetchone()["v"]
        created.append(c)
        active_now.append(a)
    return {"labels": dates, "created": created, "active": active_now}


@app.get("/api/users")
def api_users(search: str = "", status: str = "", page: int = 1, limit: int = 20):
    conn = db.get_conn()
    where = []
    params = []
    if search:
        where.append("(u.username LIKE ? OR CAST(u.telegram_id AS TEXT) LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%"])
    if status == "active":
        where.append("u.telegram_id IN (SELECT user_id FROM accounts WHERE status='active')")
    elif status == "inactive":
        where.append("u.telegram_id NOT IN (SELECT user_id FROM accounts WHERE status='active')")

    where_sql = " AND ".join(where) if where else "1"
    offset = (page - 1) * limit

    total = conn.execute(f"SELECT COUNT(*) AS v FROM users u WHERE {where_sql}", params).fetchone()["v"]
    rows = conn.execute(f"""
        SELECT u.telegram_id, u.username, u.first_name, u.created_at,
               (SELECT COUNT(*) FROM accounts WHERE user_id=u.telegram_id) as acc_count,
               (SELECT COUNT(*) FROM accounts WHERE user_id=u.telegram_id AND status='active') as active_count,
               (SELECT COALESCE(SUM(last_balance),0) FROM accounts WHERE user_id=u.telegram_id AND status='active') as total_balance,
               (SELECT COUNT(*) FROM prizes p JOIN accounts a ON p.account_id=a.id WHERE a.user_id=u.telegram_id) as total_spins,
               (SELECT COUNT(*) FROM prizes p JOIN accounts a ON p.account_id=a.id WHERE a.user_id=u.telegram_id AND p.prize_title!='') as total_bonuses,
               {_last_active_sql()} as last_active
        FROM users u
        WHERE {where_sql}
        ORDER BY COALESCE((SELECT SUM(last_balance) FROM accounts WHERE user_id=u.telegram_id AND status='active'),0) DESC
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()

    users_list = []
    for r in rows:
        last_active = ""
        if r["last_active"]:
            try:
                last_active = datetime.fromisoformat(r["last_active"]).strftime("%d.%m %H:%M")
            except Exception:
                pass
        if not last_active and r["created_at"]:
            try:
                last_active = "создан " + datetime.fromisoformat(r["created_at"]).strftime("%d.%m %H:%M")
            except Exception:
                pass
        users_list.append({
            "id": r["telegram_id"],
            "username": r["username"] or "",
            "first_name": r["first_name"] or "",
            "created_at": r["created_at"] or "",
            "accounts": r["acc_count"],
            "active_accounts": r["active_count"],
            "total_balance": r["total_balance"],
            "total_spins": r["total_spins"],
            "total_bonuses": r["total_bonuses"],
            "last_active": last_active,
        })

    return {"users": users_list, "total": total, "page": page, "pages": max(1, (total + limit - 1) // limit)}


@app.get("/api/users/{user_id}/accounts")
def api_user_accounts(user_id: int):
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT id, status, login, last_balance, last_spin, created_at
        FROM accounts WHERE user_id=?
        ORDER BY created_at DESC
    """, (user_id,)).fetchall()
    return [{
        "id": r["id"],
        "login_masked": r["login"][:2] + "•••" + r["login"][-1:] if len(r["login"]) > 3 else "•••",
        "status": r["status"],
        "balance": r["last_balance"] or 0,
        "last_spin": (datetime.fromisoformat(r["last_spin"]).strftime("%d.%m %H:%M") if r["last_spin"] else "—"),
        "created_at": (datetime.fromisoformat(r["created_at"]).strftime("%d.%m %H:%M") if r["created_at"] else ""),
    } for r in rows]


@app.get("/api/log")
def api_log(limit: int = 30):
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT p.spun_at as ts, a.login, a.user_id,
               COALESCE(p.prize_title, 'spin') as action,
               p.balance_after
        FROM prizes p
        JOIN accounts a ON p.account_id = a.id
        ORDER BY p.spun_at DESC
        LIMIT ?
    """, (limit,)).fetchall()
    return [{
        "time": (datetime.fromisoformat(r["ts"]).strftime("%d.%m %H:%M") if r["ts"] else ""),
        "user_id": r["user_id"],
        "login_masked": r["login"][:2] + "•••" + r["login"][-1:] if len(r["login"]) > 3 else "•••",
        "action": "spin" if r["action"] == "spin" else "🎁",
        "prize": r["action"] or "",
        "balance": r["balance_after"] or 0,
    } for r in rows]


@app.get("/api/top")
def api_top():
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT u.telegram_id, u.username,
               COALESCE((SELECT SUM(last_balance) FROM accounts WHERE user_id=u.telegram_id AND status='active'),0) as balance,
               (SELECT COUNT(*) FROM accounts WHERE user_id=u.telegram_id AND status='active') as accounts,
               (SELECT COUNT(*) FROM prizes p JOIN accounts a ON p.account_id=a.id WHERE a.user_id=u.telegram_id) as spins
        FROM users u
        GROUP BY u.telegram_id
        HAVING balance > 0
        ORDER BY balance DESC
        LIMIT 20
    """).fetchall()
    return [{
        "id": r["telegram_id"],
        "username": (r["username"] or f"ID:{r['telegram_id']}"),
        "balance": r["balance"],
        "accounts": r["accounts"],
        "spins": r["spins"],
    } for r in rows]


@app.post("/api/broadcast")
def api_broadcast(data: dict):
    text = data.get("text", "")
    mode = data.get("mode", "all")
    photo = data.get("photo", "")  # URL or Telegram file_id
    if not TG_BOT_TOKEN:
        return {"ok": False, "error": "TG_BOT_TOKEN not set"}
    conn = db.get_conn()
    if mode == "active":
        rows = conn.execute("SELECT DISTINCT user_id AS telegram_id FROM accounts WHERE status='active'").fetchall()
    else:
        rows = conn.execute("SELECT telegram_id FROM users").fetchall()
    sent = 0
    failed = 0
    for r in rows:
        try:
            if photo:
                payload = {"chat_id": r["telegram_id"], "photo": photo, "parse_mode": "HTML"}
                if text:
                    payload["caption"] = text
                resp = requests.post(
                    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                    json=payload,
                    timeout=15,
                )
            else:
                resp = requests.post(
                    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                    json={"chat_id": r["telegram_id"], "text": text, "parse_mode": "HTML"},
                    timeout=15,
                )
            if resp.status_code == 200:
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    add_log("admin", "broadcast", f"sent={sent} failed={failed}")
    return {"ok": True, "sent": sent, "failed": failed}


@app.get("/api/dashboard/log")
def api_dashboard_log():
    with _log_lock:
        return list(_log_buffer)


@app.get("/api/tracking")
def api_tracking():
    tracker = db.get_tracking_account()
    log = db.get_tracking_log(50)
    t = None
    if tracker:
        t = {
            "id": tracker["id"],
            "login": tracker["login"] or "?",
            "last_balance": tracker["last_balance"],
            "last_checked": (
                datetime.fromisoformat(tracker["last_checked"]).strftime("%d.%m %H:%M:%S")
                if tracker["last_checked"] else ""
            ),
            "status": tracker["status"] or "active",
        }
    entries = []
    for r in log:
        entries.append({
            "time": (
                datetime.fromisoformat(r["event_time"]).strftime("%d.%m %H:%M:%S")
                if r["event_time"] else ""
            ),
            "message": r["message"],
        })
    return {"tracker": t, "log": entries}


@app.post("/api/tracking/add")
def api_tracking_add(data: dict):
    token = (data.get("token") or "").strip()
    if not token.startswith("y0__"):
        return {"ok": False, "error": "Токен должен начинаться с y0__"}

    yandex_check = edadeal.check_yandex_token(token)
    if not yandex_check["ok"]:
        return {"ok": False, "error": f"Яндекс-токен недействителен: {yandex_check['error']}"}

    auth = edadeal.authenticate(token)
    if not auth["ok"]:
        return {"ok": False, "error": f"Ошибка авторизации Едадил: {auth['error']}"}

    db.set_tracking_account(
        token,
        login=yandex_check["login"],
        duid=auth.get("duid", ""),
        edadeal_uid=auth.get("uid", ""),
    )
    db.add_tracking_log(f"🆕 Отслеживающий аккаунт добавлен: {yandex_check['login']}")
    add_log("admin", "tracking_add", yandex_check["login"])
    return {"ok": True, "login": yandex_check["login"]}


@app.post("/api/tracking/delete")
def api_tracking_delete():
    tracker = db.get_tracking_account()
    if tracker:
        db.add_tracking_log(f"🗑 Отслеживающий аккаунт удалён: {tracker['login']}")
        add_log("admin", "tracking_delete", tracker["login"])
    db.delete_tracking_account()
    return {"ok": True}


@app.get("/api/tracking/triggers/last")
def api_tracking_triggers_last():
    log = db.get_tracking_log(50)
    rows = [r for r in log if "Триггеры" in r["message"]]
    if not rows:
        return {"ok": False, "error": "Триггеры ещё не запускались"}
    return {"ok": True, "message": rows[0]["message"]}


HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Edadeal Bot — Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0b0d14;--surface:#151923;--surface2:#1b2030;--border:#232a3d;--text:#e4e9f2;--text2:#8d97af;--text3:#586077;--blue:#4f8aff;--green:#34d399;--yellow:#fbbf24;--purple:#a78bfa;--pink:#f472b6;--red:#f87171;--cyan:#22d3ee}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',Roboto,sans-serif;display:flex;min-height:100vh;font-size:16px;line-height:1.5}
.sidebar{width:250px;background:var(--surface);border-right:1px solid var(--border);padding:30px 18px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh}
.logo{font-size:21px;font-weight:700;color:#fff;margin-bottom:36px;display:flex;align-items:center;gap:14px}
.logo .icon{width:38px;height:38px;background:linear-gradient(135deg,var(--blue),var(--purple));border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:18px;box-shadow:0 0 20px rgba(79,138,255,.25)}
.nav{display:flex;flex-direction:column;gap:4px;flex:1}
.nav a{color:var(--text2);text-decoration:none;padding:13px 16px;border-radius:12px;font-size:15px;font-weight:500;display:flex;align-items:center;gap:12px;transition:all .25s cubic-bezier(.4,0,.2,1);cursor:pointer}
.nav a:hover{background:var(--surface2);color:var(--text);transform:translateX(5px)}
.nav a.active{background:linear-gradient(135deg,rgba(79,138,255,.15),transparent);color:var(--blue);border-left:3px solid var(--blue);border-radius:12px 8px 8px 12px;font-weight:600}
.main{flex:1;padding:32px 36px;max-width:1280px;overflow-y:auto;max-height:100vh;width:0}
.header-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:32px;flex-wrap:wrap;gap:12px}
.header-bar h1{font-size:28px;font-weight:700;letter-spacing:-.3px}
.header-bar .sub{font-size:14px;color:var(--text3)}
.btn{padding:10px 22px;border-radius:12px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-size:14px;font-weight:500;cursor:pointer;transition:all .25s;display:inline-flex;align-items:center;gap:8px;user-select:none}
.btn:hover{background:var(--surface2);border-color:var(--text3);transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,.3)}
.btn:active{transform:translateY(0)}
.btn-primary{background:linear-gradient(135deg,var(--blue),#3b6fe0);border:none;color:#fff;padding:10px 26px}
.btn-primary:hover{opacity:.92;transform:translateY(-2px);box-shadow:0 4px 20px rgba(79,138,255,.3)}
.page{display:none;opacity:0;transition:opacity .4s ease,transform .4s cubic-bezier(.4,0,.2,1);transform:translateY(15px)}
.page.active{display:block;opacity:1;transform:translateY(0)}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;margin-bottom:32px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:24px 26px;transition:all .35s cubic-bezier(.4,0,.2,1);position:relative;overflow:hidden;cursor:default}
.stat-card:hover{border-color:rgba(255,255,255,.08);transform:translateY(-3px);box-shadow:0 8px 32px rgba(0,0,0,.4)}
.stat-card .num{font-size:32px;font-weight:800;line-height:1.15;letter-spacing:-.5px;transition:transform .3s}
.stat-card:hover .num{transform:scale(1.04)}
.stat-card .label{font-size:14px;color:var(--text2);margin-top:6px;font-weight:500}
.stat-card .sub{font-size:12px;color:var(--text3);margin-top:3px}
.stat-card .glow{position:absolute;top:-50%;right:-30%;width:120px;height:120px;border-radius:50%;opacity:.07;pointer-events:none;transition:opacity .5s}
.stat-card:hover .glow{opacity:.14}
.charts-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:32px}
.chart-card{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:24px;position:relative;transition:border-color .3s}
.chart-card:hover{border-color:rgba(255,255,255,.06)}
.chart-card h3{font-size:15px;color:var(--text2);margin-bottom:16px;font-weight:600;display:flex;align-items:center;gap:8px}
.chart-card canvas{max-height:280px}
.chart-card.full{grid-column:1/-1}
.charts-grid .chart-card{animation:fadeSlide .5s ease both}
.charts-grid .chart-card:nth-child(1){animation-delay:0s}
.charts-grid .chart-card:nth-child(2){animation-delay:.12s}
.charts-grid .chart-card:nth-child(3){animation-delay:.24s}
.charts-grid .chart-card:nth-child(4){animation-delay:.36s}
.filters{display:flex;gap:12px;margin-bottom:20px;flex-wrap:wrap;align-items:center}
.filters input,.filters select,.filters textarea{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 14px;color:var(--text);font-size:14px;outline:none;transition:border-color .25s,box-shadow .25s}
.filters input:focus,.filters select:focus,.filters textarea:focus{border-color:var(--blue);box-shadow:0 0 0 3px rgba(79,138,255,.12)}
.filters input{width:220px}
.filters textarea{width:100%;min-height:140px;resize:vertical;font-family:inherit}
.table-wrap{background:var(--surface);border:1px solid var(--border);border-radius:16px;overflow:hidden;margin-bottom:32px;animation:fadeSlide .45s ease}
.table-wrap table{width:100%;border-collapse:collapse;font-size:14px}
.table-wrap th{background:var(--surface2);color:var(--text2);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.6px;text-align:left;padding:14px 18px;border-bottom:1px solid var(--border)}
.table-wrap td{padding:14px 18px;border-bottom:1px solid var(--border);vertical-align:middle}
.table-wrap tr:last-child td{border-bottom:0}
.table-wrap tr{transition:background .15s}
.table-wrap tbody tr:hover td{background:rgba(79,138,255,.04)}
.status-dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:8px;vertical-align:middle}
.status-dot.on{background:var(--green);box-shadow:0 0 10px rgba(52,211,153,.5)}
.status-dot.off{background:var(--text3)}
.expand-btn{cursor:pointer;color:var(--text3);transition:transform .3s,color .2s;font-size:14px;user-select:none;display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px}
.expand-btn:hover{color:var(--blue)}
.expand-btn.open{transform:rotate(90deg);color:var(--blue)}
.sub-row{display:none}
.sub-row.open{display:table-row;animation:fadeSlide .3s ease}
.sub-table td{padding:10px 18px 10px 56px;font-size:14px;border-bottom:1px solid var(--border)}
.sub-table .masked{color:var(--text3);font-family:monospace;letter-spacing:2px;font-size:13px}
.sub-table .bal{color:var(--yellow);font-weight:700}
.sub-table .st{font-size:12px;font-weight:500}
.sub-table .st.active{color:var(--green)}
.sub-table .st.expired{color:var(--red)}
.pagination{display:flex;justify-content:space-between;align-items:center;padding:14px 18px;border-top:1px solid var(--border);font-size:14px;color:var(--text2)}
.pagination .pages{display:flex;gap:4px}
.pagination .pages span{width:34px;height:34px;display:flex;align-items:center;justify-content:center;border-radius:8px;cursor:pointer;font-size:14px;transition:all .2s}
.pagination .pages span:hover{background:var(--surface2)}
.pagination .pages span.active{background:var(--blue);color:#fff}
.log-card{background:var(--surface);border:1px solid var(--border);border-radius:16px;overflow:hidden}
.log-item{display:flex;align-items:center;gap:12px;padding:12px 18px;border-bottom:1px solid var(--border);font-size:14px;transition:background .15s}
.log-item:last-child{border-bottom:0}
.log-item:hover{background:var(--surface2)}
.log-item .ltime{color:var(--text3);font-family:monospace;font-size:12px;min-width:80px;white-space:nowrap}
.log-item .lbadge{font-size:11px;font-weight:600;padding:4px 10px;border-radius:14px;white-space:nowrap}
.log-item .lbadge.spin{background:rgba(251,191,36,.12);color:var(--yellow)}
.log-item .lbadge.bonus{background:rgba(52,211,153,.12);color:var(--green)}
.log-item .lbadge.broadcast{background:rgba(79,138,255,.12);color:var(--blue)}
.log-item .lmsg{flex:1;color:var(--text2)}
.log-item .lmsg b{color:var(--text)}
.leaderboard{list-style:none}
.leaderboard li{display:flex;align-items:center;padding:12px 18px;border-bottom:1px solid var(--border);gap:12px;font-size:14px;transition:background .15s;animation:fadeSlide .35s ease both}
.leaderboard li:last-child{border-bottom:0}
.leaderboard li:hover{background:var(--surface2)}
.leaderboard li:nth-child(1){animation-delay:0s}
.leaderboard li:nth-child(2){animation-delay:.05s}
.leaderboard li:nth-child(3){animation-delay:.10s}
.leaderboard li:nth-child(4){animation-delay:.15s}
.leaderboard li:nth-child(5){animation-delay:.20s}
.leaderboard li:nth-child(6){animation-delay:.25s}
.leaderboard li:nth-child(7){animation-delay:.30s}
.leaderboard li:nth-child(8){animation-delay:.35s}
.leaderboard li:nth-child(9){animation-delay:.40s}
.leaderboard li:nth-child(10){animation-delay:.45s}
.leaderboard .pos{width:28px;height:28px;border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;background:var(--surface2);color:var(--text2);transition:all .3s}
.leaderboard .pos.gold{background:rgba(251,191,36,.15);color:var(--yellow);box-shadow:0 0 12px rgba(251,191,36,.2)}
.leaderboard .pos.silver{background:rgba(192,192,192,.12);color:#c0c0c0}
.leaderboard .pos.bronze{background:rgba(205,127,50,.12);color:#cd7f32}
.leaderboard .lname{flex:1;color:var(--text);font-weight:500}
.leaderboard .lscore{color:var(--yellow);font-weight:700;font-size:15px}
.leaderboard .lmeta{color:var(--text3);font-size:12px}
.broadcast-box{background:var(--surface);border:1px solid var(--border);border-radius:16px;padding:28px;max-width:680px;animation:fadeSlide .4s ease}
.broadcast-box label{display:block;font-size:14px;color:var(--text2);margin-bottom:6px;margin-top:16px;font-weight:500}
.broadcast-box label:first-child{margin-top:0}
.broadcast-result{margin-top:16px;padding:14px 18px;border-radius:12px;font-size:14px;display:none;animation:fadeSlide .3s ease}
.broadcast-result.ok{display:block;background:rgba(52,211,153,.1);color:var(--green);border:1px solid rgba(52,211,153,.2)}
.broadcast-result.fail{display:block;background:rgba(248,113,113,.1);color:var(--red);border:1px solid rgba(248,113,113,.2)}
.user-info .name{font-weight:600;font-size:15px}
.user-info .sub{font-size:12px;color:var(--text3);font-family:monospace;margin-top:2px;display:block}
td .bal-val{color:var(--yellow);font-weight:700}
.empty{text-align:center;padding:50px 20px;color:var(--text3);font-size:15px}
@keyframes fadeSlide{from{opacity:0;transform:translateY(15px)}to{opacity:1;transform:translateY(0)}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes pulseGlow{0%,100%{opacity:.07}50%{opacity:.16}}
@keyframes countPop{0%{transform:scale(.3);opacity:0}60%{transform:scale(1.08)}100%{transform:scale(1);opacity:1}}
@keyframes shimmer{0%{background-position:-200% 0}100%{background-position:200% 0}}
@keyframes slideUp{from{opacity:0;transform:translateY(20px) scale(.98)}to{opacity:1;transform:translateY(0) scale(1)}}
.stat-card{animation:slideUp .5s cubic-bezier(.4,0,.2,1) both}
.stat-card:nth-child(1){animation-delay:.02s}
.stat-card:nth-child(2){animation-delay:.07s}
.stat-card:nth-child(3){animation-delay:.12s}
.stat-card:nth-child(4){animation-delay:.17s}
.stat-card:nth-child(5){animation-delay:.22s}
.stat-card:nth-child(6){animation-delay:.27s}
.stat-card:nth-child(7){animation-delay:.32s}
.stat-card .glow{animation:pulseGlow 3.5s ease-in-out infinite}
.stat-card .num .count-up{display:inline-block}
.log-item{animation:fadeSlide .35s ease both}
.log-item:nth-child(1){animation-delay:0s}
.log-item:nth-child(2){animation-delay:.04s}
.log-item:nth-child(3){animation-delay:.08s}
.log-item:nth-child(4){animation-delay:.12s}
.log-item:nth-child(5){animation-delay:.16s}
.log-item:nth-child(6){animation-delay:.20s}
.loading{background:linear-gradient(90deg,var(--surface) 25%,var(--surface2) 50%,var(--surface) 75%);background-size:200% 100%;animation:shimmer 1.5s ease-in-out infinite;border-radius:8px;height:20px;margin:4px 0}
.theme-toggle{background:none;border:none;color:var(--text3);cursor:pointer;font-size:18px;padding:6px;border-radius:8px;transition:all .2s}
.theme-toggle:hover{background:var(--surface2);color:var(--text)}
@media(max-width:1024px){
  .charts-grid{grid-template-columns:1fr}
  .sidebar{width:200px;padding:24px 14px}
  .sidebar .nav a{font-size:14px;padding:11px 14px}
}
@media(max-width:768px){
  .sidebar{width:56px;padding:16px 8px}
  .sidebar .logo span,.sidebar .nav a span{display:none}
  .sidebar .logo{justify-content:center;gap:0}
  .sidebar .logo .icon{width:32px;height:32px;font-size:15px}
  .sidebar .nav a{justify-content:center;padding:10px;font-size:16px;border-radius:8px}
  .sidebar .nav a.active{border-left:none}
  .main{padding:20px 16px}
  .charts-grid{grid-template-columns:1fr}
  .stats{grid-template-columns:repeat(2,1fr)}
  .stat-card{padding:18px}
  .stat-card .num{font-size:26px}
  .header-bar h1{font-size:22px}
  .filters input{width:100%}
  .table-wrap{overflow-x:auto}
  .table-wrap table{font-size:13px}
  .table-wrap th,.table-wrap td{padding:10px 12px}
}
</style>
</head>
<body>
<aside class="sidebar">
  <div class="logo"><div class="icon">🎡</div><span>Edadeal Bot</span></div>
  <div class="nav">
    <a class="active" data-page="dashboard"><span>📊</span><span>Дашборд</span></a>
    <a data-page="users"><span>👥</span><span>Пользователи</span></a>
    <a data-page="analytics"><span>📈</span><span>Аналитика</span></a>
    <a data-page="tracking"><span>🔍</span><span>Трекинг</span></a>
    <a data-page="broadcast"><span>📨</span><span>Рассылка</span></a>
    <a data-page="system"><span>⚙️</span><span>Система</span></a>
  </div>
  <div style="padding-top:16px;border-top:1px solid var(--border);margin-top:auto;font-size:13px;color:var(--text3);text-align:center">
    v3.0 · <span id="clock" style="font-family:monospace"></span>
  </div>
</aside>

<div class="main" id="app">
  <div class="page active" id="page-dashboard">
    <div class="header-bar">
      <div><h1>📊 Дашборд</h1><span class="sub">Общая статистика бота</span></div>
      <button class="btn" onclick="refreshDashboard()">🔄 Обновить</button>
    </div>
    <div class="stats" id="stats"></div>
    <div class="charts-grid">
      <div class="chart-card"><h3>📈 Регистрации (30 дней)</h3><canvas id="chartTimeline"></canvas></div>
      <div class="chart-card"><h3>🎡 Активность (14 дней)</h3><canvas id="chartDaily"></canvas></div>
      <div class="chart-card"><h3>💎 Распределение алмазов</h3><canvas id="chartBalances"></canvas></div>
      <div class="chart-card"><h3>🏆 Топ пользователей</h3>
        <div id="topList"><div class="empty">Загрузка...</div></div>
      </div>
    </div>
    <div style="margin-top:8px;font-size:13px;color:var(--text3);text-align:center">⏱ Обновление каждые 15с</div>
  </div>

  <div class="page" id="page-users">
    <div class="header-bar">
      <div><h1>👥 Пользователи</h1><span class="sub">Управление и просмотр</span></div>
    </div>
    <div class="filters">
      <input type="text" id="search" placeholder="🔍 Поиск по username или ID..." oninput="loadUsersDelayed()">
      <select id="statusFilter" onchange="loadUsers()">
        <option value="">Все статусы</option>
        <option value="active">Только активные</option>
        <option value="inactive">Неактивные</option>
      </select>
      <span id="usersCount" style="font-size:13px;color:var(--text3);margin-left:auto"></span>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th style="width:22px"></th>
          <th>Пользователь</th>
          <th>Аккаунты</th>
          <th>Актив</th>
          <th>💎 Алмазы</th>
          <th>🎡 Прокрутки</th>
          <th>🏆 Призы</th>
          <th>Последний раз</th>
        </tr></thead>
        <tbody id="usersBody"></tbody>
      </table>
      <div class="pagination">
        <span id="paginationInfo">Загрузка...</span>
        <div class="pages" id="paginationPages"></div>
      </div>
    </div>
  </div>

  <div class="page" id="page-analytics">
    <div class="header-bar">
      <div><h1>📈 Аналитика</h1><span class="sub">Детальные графики и метрики</span></div>
      <button class="btn" onclick="refreshAnalytics()">🔄 Обновить</button>
    </div>
    <div class="charts-grid">
      <div class="chart-card full"><h3>📊 Жизненный цикл аккаунтов (30 дней)</h3><canvas id="chartRetention"></canvas></div>
      <div class="chart-card"><h3>🎁 Типы бонусов</h3><canvas id="chartBonuses"></canvas></div>
      <div class="chart-card"><h3>📅 Ежедневные чекины</h3><canvas id="chartCheckins"></canvas></div>
    </div>
  </div>

  <div class="page" id="page-tracking">
    <div class="header-bar">
      <div><h1>🔍 Трекинг</h1><span class="sub">Отслеживающий аккаунт: меняется баланс → триггеры на все аккаунты</span></div>
      <button class="btn" onclick="loadTracking()">🔄 Обновить</button>
    </div>
    <div class="broadcast-box">
      <label>Яндекс-токен отслеживающего аккаунта (y0__...)</label>
      <input id="trackingToken" type="text" placeholder="y0__..." style="width:100%;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 14px;color:var(--text);font-size:14px;font-family:monospace">
      <div style="margin-top:16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap">
        <button class="btn btn-primary" onclick="addTracking()">➕ Добавить аккаунт</button>
        <button class="btn" onclick="deleteTracking()" style="border-color:rgba(248,113,113,.4);color:var(--red)">🗑 Удалить</button>
        <span id="trackingStatus" style="font-size:14px;color:var(--text2)"></span>
      </div>
    </div>
    <div class="stats" id="trackingInfo" style="margin-top:24px"></div>
    <div class="log-card" id="trackingLog" style="max-width:700px"></div>
  </div>

  <div class="page" id="page-broadcast">
    <div class="header-bar">
      <div><h1>📨 Рассылка</h1><span class="sub">Отправка сообщений пользователям</span></div>
    </div>
    <div class="broadcast-box">
      <label>Кому отправляем</label>
      <select id="broadcastMode" style="padding:10px 14px;width:auto;background:var(--surface);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:14px;margin-bottom:16px">
        <option value="all">Всем пользователям</option>
        <option value="active">Только активным</option>
      </select>
      <label>Ссылка на изображение (необязательно)</label>
      <input id="broadcastPhoto" type="text" placeholder="https://example.com/image.jpg или file_id..." style="width:100%;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 14px;color:var(--text);font-size:14px">
      <label>Текст сообщения (HTML)</label>
      <textarea id="broadcastText" placeholder="Напишите сообщение..." style="width:100%;min-height:140px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px;color:var(--text);font-size:14px;resize:vertical;font-family:inherit"></textarea>
      <div style="margin-top:16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap">
        <button class="btn btn-primary" onclick="sendBroadcast()">📨 Отправить</button>
        <span id="broadcastStatus" style="font-size:14px;color:var(--text2)"></span>
      </div>
      <div class="broadcast-result" id="broadcastResult"></div>
    </div>
  </div>

  <div class="page" id="page-system">
    <div class="header-bar">
      <div><h1>⚙️ Система</h1><span class="sub">Мониторинг и логи</span></div>
      <button class="btn" onclick="loadSystem()">🔄 Обновить</button>
    </div>
    <div class="stats" id="sysStats"></div>
    <div class="log-card" id="sysLog" style="max-width:700px"></div>
  </div>
</div>

<script>
var charts = {};
var currentPage = 1;
var TIMERS = [];
var searchTimeout = null;

function switchPage(name) {
  document.querySelectorAll('.page').forEach(function(p) { p.classList.remove('active'); });
  document.getElementById('page-' + name).classList.add('active');
  document.querySelectorAll('.nav a').forEach(function(a) { a.classList.remove('active'); });
  document.querySelector('.nav a[data-page="' + name + '"]').classList.add('active');
  TIMERS.forEach(function(t) { clearInterval(t); });
  TIMERS = [];
  if (name === 'dashboard') { refreshDashboard(); TIMERS.push(setInterval(refreshDashboard, 15000)); }
  if (name === 'users') { loadUsers(); }
  if (name === 'analytics') { refreshAnalytics(); }
  if (name === 'tracking') { loadTracking(); }
  if (name === 'system') { loadSystem(); }
}

document.querySelectorAll('.nav a').forEach(function(a) {
  a.onclick = function() { switchPage(this.dataset.page); };
});

function updateClock() {
  var d = new Date();
  document.getElementById('clock').textContent = d.toLocaleTimeString('ru-RU', {hour:'2-digit',minute:'2-digit'});
}
setInterval(updateClock, 30000);
updateClock();

async function api(path) {
  try { var r = await fetch(path); return await r.json(); }
  catch(e) { return null; }
}

function animateNum(el, target, suffix) {
  var start = 0;
  var dur = 800;
  var step = Math.max(1, Math.floor(target / 30));
  var interval = Math.floor(dur / (target / step || 1));
  if (interval < 5) { interval = 5; step = Math.ceil(target / 20); }
  var cur = start;
  var timer = setInterval(function() {
    cur += step;
    if (cur >= target) { cur = target; clearInterval(timer); }
    el.textContent = (suffix ? cur.toLocaleString() + ' ' + suffix : cur.toLocaleString());
  }, interval);
}

function countPop(el) { el.style.animation = 'none'; void el.offsetHeight; el.style.animation = 'countPop .5s ease'; }

// === DASHBOARD ===
async function refreshDashboard() {
  await Promise.all([loadStats(), loadCharts(), loadTop()]);
}

async function loadStats() {
  var d = await api('/api/stats');
  if (!d) return;
  document.getElementById('stats').innerHTML =
    '<div class="stat-card"><div class="glow" style="background:var(--blue)"></div><div class="num" style="color:var(--blue)"><span class="count-up" id="statUsers">'+d.users+'</span></div><div class="label">👥 Пользователей</div><div class="sub">+'+d.today_users+' сегодня</div></div>' +
    '<div class="stat-card"><div class="glow" style="background:var(--green)"></div><div class="num" style="color:var(--green)"><span class="count-up">'+d.total_accounts+'</span></div><div class="label">📦 Всего аккаунтов</div><div class="sub">+'+d.today_accounts+' сегодня</div></div>' +
    '<div class="stat-card"><div class="glow" style="background:var(--yellow)"></div><div class="num" style="color:var(--yellow)"><span class="count-up">'+d.active_accounts+'</span></div><div class="label">✅ Активных</div><div class="sub">'+d.expired_accounts+' истекших</div></div>' +
    '<div class="stat-card"><div class="glow" style="background:var(--purple)"></div><div class="num" style="color:var(--purple)"><span class="count-up">'+d.active_users+'</span></div><div class="label">👤 Активных юзеров</div></div>' +
    '<div class="stat-card"><div class="glow" style="background:var(--pink)"></div><div class="num" style="color:var(--pink)"><span class="count-up">'+d.total_balance.toLocaleString()+'</span> <span style="font-size:18px">💎</span></div><div class="label">💰 Всего алмазов</div><div class="sub">среднее '+(d.active_accounts ? Math.round(d.total_balance/d.active_accounts).toLocaleString() : 0)+' на акк</div></div>' +
    '<div class="stat-card"><div class="glow" style="background:var(--cyan)"></div><div class="num" style="color:var(--cyan)"><span class="count-up">'+d.total_spins+'</span></div><div class="label">🎡 Прокруток</div><div class="sub">'+d.total_bonuses+' призов · '+d.today_checkins+' чекинов сегодня</div></div>';
}

async function loadCharts() {
  var tl = await api('/api/stats/timeline?days=30');
  var da = await api('/api/stats/daily?days=14');
  var ba = await api('/api/stats/balances');
  if (!tl || !da || !ba) return;

  var colors = {blue:'#4f8aff',green:'#34d399',yellow:'#fbbf24',purple:'#a78bfa',pink:'#f472b6',red:'#f87171',cyan:'#22d3ee'};

  if (charts.timeline) charts.timeline.destroy();
  if (charts.daily) charts.daily.destroy();
  if (charts.balances) charts.balances.destroy();

  var ctx1 = document.getElementById('chartTimeline').getContext('2d');
  charts.timeline = new Chart(ctx1, {
    type: 'line',
    data: {
      labels: tl.labels.map(function(d) { return d.slice(5); }),
      datasets: [
        { label: 'Пользователи', data: tl.users, borderColor: colors.blue, backgroundColor: colors.blue + '18', fill: true, tension: .4, pointRadius: 2, pointHoverRadius: 5, borderWidth: 2.5 },
        { label: 'Аккаунты', data: tl.accounts, borderColor: colors.green, backgroundColor: colors.green + '18', fill: true, tension: .4, pointRadius: 2, pointHoverRadius: 5, borderWidth: 2.5 },
        { label: 'Активные', data: tl.active, borderColor: colors.yellow, backgroundColor: colors.yellow + '18', fill: true, tension: .4, pointRadius: 2, pointHoverRadius: 5, borderWidth: 2.5 }
      ]
    },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, plugins: { legend: { labels: { color: '#8d97af', font: { size: 12 }, usePointStyle: true, padding: 16 } } }, scales: { x: { ticks: { color: '#586077', maxTicksLimit: 10, font: { size: 11 } }, grid: { color: '#232a3d' } }, y: { ticks: { color: '#586077', font: { size: 11 } }, grid: { color: '#232a3d' }, beginAtZero: true } } }
  });

  var ctx2 = document.getElementById('chartDaily').getContext('2d');
  charts.daily = new Chart(ctx2, {
    type: 'bar',
    data: {
      labels: da.labels.map(function(d) { return d.slice(5); }),
      datasets: [
        { label: 'Прокрутки', data: da.spins, backgroundColor: colors.purple + '55', borderColor: colors.purple, borderWidth: 1.5, borderRadius: 4 },
        { label: 'Чекины', data: da.checkins, backgroundColor: colors.green + '55', borderColor: colors.green, borderWidth: 1.5, borderRadius: 4 }
      ]
    },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, plugins: { legend: { labels: { color: '#8d97af', font: { size: 12 }, usePointStyle: true } } }, scales: { x: { ticks: { color: '#586077', font: { size: 11 } }, grid: { color: '#232a3d' } }, y: { ticks: { color: '#586077', font: { size: 11 } }, grid: { color: '#232a3d' }, beginAtZero: true } } }
  });

  var ctx3 = document.getElementById('chartBalances').getContext('2d');
  charts.balances = new Chart(ctx3, {
    type: 'doughnut',
    data: {
      labels: ba.labels,
      datasets: [{ data: ba.values, backgroundColor: [colors.red + '77', colors.yellow + '77', colors.blue + '77', colors.purple + '77', colors.green + '77'], borderWidth: 2 }]
    },
    options: { responsive: true, maintainAspectRatio: false, cutout: '60%', plugins: { legend: { position: 'right', labels: { color: '#8d97af', font: { size: 12 }, padding: 14, usePointStyle: true } } } }
  });
}

async function loadTop() {
  var d = await api('/api/top');
  if (!d) return;
  var el = document.getElementById('topList');
  if (!d.length) { el.innerHTML = '<div class="empty">Нет данных</div>'; return; }
  var medals = ['gold','silver','bronze'];
  var icons = ['🥇','🥈','🥉'];
  el.innerHTML = '<ol class="leaderboard">' + d.map(function(u, i) {
    var cls = i < 3 ? medals[i] : '';
    var icon = i < 3 ? icons[i] : (i+1);
    return '<li><span class="pos ' + cls + '">' + icon + '</span>' +
      '<span class="lname">' + u.username + '</span>' +
      '<span class="lmeta">' + u.accounts + ' акк · ' + u.spins + ' спин</span>' +
      '<span class="lscore">' + u.balance.toLocaleString() + ' 💎</span></li>';
  }).join('') + '</ol>';
}

// === USERS ===
function loadUsersDelayed() {
  clearTimeout(searchTimeout);
  searchTimeout = setTimeout(loadUsers, 300);
}

async function loadUsers() {
  var search = document.getElementById('search').value;
  var status = document.getElementById('statusFilter').value;
  var d = await api('/api/users?search='+encodeURIComponent(search)+'&status='+status+'&page='+currentPage+'&limit=20');
  if (!d) { document.getElementById('usersBody').innerHTML = '<tr><td colspan="8" class="empty">Ошибка загрузки</td></tr>'; return; }
  var tbody = document.getElementById('usersBody');
  tbody.innerHTML = '';
  if (!d.users.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty">Нет пользователей</td></tr>';
    document.getElementById('paginationInfo').textContent = '0 найдено';
    document.getElementById('paginationPages').innerHTML = '';
    document.getElementById('usersCount').textContent = '';
    return;
  }
  d.users.forEach(function(u) {
    var tr = document.createElement('tr');
    var activeDot = u.active_accounts > 0 ? 'on' : 'off';
    var name = u.first_name ? u.first_name + ' ' : '';
    name += u.username ? '@' + u.username : 'ID: ' + u.id;
    var lastActive = u.last_active || '—';
    tr.innerHTML = '<td><span class="expand-btn" data-uid="'+u.id+'">▶</span></td>' +
      '<td><div class="user-info"><span class="name"><span class="status-dot '+activeDot+'"></span>' +
      name + '</span><span class="sub">ID: ' + u.id + '</span></div></td>' +
      '<td>'+u.accounts+'</td><td>'+u.active_accounts+'</td>' +
      '<td class="bal-val">'+u.total_balance.toLocaleString()+'</td>' +
      '<td>'+u.total_spins+'</td><td style="color:var(--green);font-weight:600">'+u.total_bonuses+'</td>' +
      '<td style="font-size:13px;color:var(--text2)">'+lastActive+'</td>';
    tbody.appendChild(tr);
  });
  document.getElementById('paginationInfo').textContent = 'Показано '+d.users.length+' из '+d.total;
  document.getElementById('usersCount').textContent = 'Всего: '+d.total;
  var pp = document.getElementById('paginationPages');
  pp.innerHTML = '';
  var maxPages = Math.min(d.pages, 10);
  for (var i = 1; i <= maxPages; i++) {
    var s = document.createElement('span');
    s.textContent = i;
    if (i === currentPage) s.className = 'active';
    s.onclick = function(p) { return function() { currentPage = p; loadUsers(); }; }(i);
    pp.appendChild(s);
  }
}

document.addEventListener('click', function(e) {
  var btn = e.target.closest('.expand-btn');
  if (!btn) return;
  var uid = btn.dataset.uid;
  var row = btn.closest('tr');
  var next = row.nextElementSibling;
  if (next && next.classList.contains('sub-row')) {
    next.remove();
    btn.classList.remove('open');
    return;
  }
  btn.classList.add('open');
  fetch('/api/users/'+uid+'/accounts').then(function(r) { return r.json(); }).then(function(accs) {
    var tr = document.createElement('tr');
    tr.className = 'sub-row open';
    tr.innerHTML = '<td colspan="8" style="padding:0"><table style="width:100%">' +
      accs.map(function(a) {
        var stCls = a.status === 'active' ? 'active' : 'expired';
        var stIcon = a.status === 'active' ? '✅' : '⛔';
        return '<tr class="sub-table"><td><span class="masked">'+a.login_masked+'</span></td>' +
          '<td><span class="st '+stCls+'">'+stIcon+' '+a.status+'</span></td>' +
          '<td class="bal">'+a.balance.toLocaleString()+' 💎</td>' +
          '<td style="font-size:13px;color:var(--text2)">🔄 '+(a.last_spin||'—')+'</td>' +
          '<td style="font-size:12px;color:var(--text3)">📅 '+(a.created_at||'')+'</td></tr>';
      }).join('') +
      '</table></td>';
    row.after(tr);
  });
});

// === ANALYTICS ===
async function refreshAnalytics() {
  await Promise.all([loadRetentionChart(), loadBonusChart(), loadCheckinChart()]);
}

async function loadRetentionChart() {
  var d = await api('/api/stats/retention?days=30');
  if (!d) return;
  if (charts.retention) charts.retention.destroy();
  var ctx = document.getElementById('chartRetention').getContext('2d');
  charts.retention = new Chart(ctx, {
    type: 'line',
    data: {
      labels: d.labels.map(function(x) { return x.slice(5); }),
      datasets: [
        { label: 'Создано аккаунтов', data: d.created, borderColor: '#4f8aff', backgroundColor: 'rgba(79,138,255,.08)', fill: true, tension: .4, pointRadius: 2, borderWidth: 2.5 },
        { label: 'Активных', data: d.active, borderColor: '#34d399', backgroundColor: 'rgba(52,211,153,.08)', fill: true, tension: .4, pointRadius: 2, borderWidth: 2.5 }
      ]
    },
    options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, plugins: { legend: { labels: { color: '#8d97af', font: { size: 12 }, usePointStyle: true } } }, scales: { x: { ticks: { color: '#586077', maxTicksLimit: 10, font: { size: 11 } }, grid: { color: '#232a3d' } }, y: { ticks: { color: '#586077', font: { size: 11 } }, grid: { color: '#232a3d' }, beginAtZero: true } } }
  });
}

async function loadBonusChart() {
  var d = await api('/api/stats/bonuses');
  if (!d) return;
  if (charts.bonuses) charts.bonuses.destroy();
  var colors = ['#fbbf24','#34d399','#4f8aff','#a78bfa','#f472b6','#22d3ee','#f87171','#fb923c','#a3e635','#e879f9'];
  var ctx = document.getElementById('chartBonuses').getContext('2d');
  charts.bonuses = new Chart(ctx, {
    type: 'doughnut',
    data: {
      labels: d.labels.map(function(l) { return l.length > 25 ? l.slice(0,25)+'…' : l; }),
      datasets: [{ data: d.values, backgroundColor: colors.slice(0,d.labels.length).map(function(c) { return c+'77'; }), borderWidth: 2 }]
    },
    options: { responsive: true, maintainAspectRatio: false, cutout: '55%', plugins: { legend: { position: 'right', labels: { color: '#8d97af', font: { size: 11 }, padding: 10, usePointStyle: true } } } }
  });
}

async function loadCheckinChart() {
  var d = await api('/api/stats/daily?days=14');
  if (!d) return;
  if (charts.checkins) charts.checkins.destroy();
  var ctx = document.getElementById('chartCheckins').getContext('2d');
  charts.checkins = new Chart(ctx, {
    type: 'line',
    data: {
      labels: d.labels.map(function(x) { return x.slice(5); }),
      datasets: [
        { label: 'Чекины', data: d.checkins, borderColor: '#34d399', backgroundColor: 'rgba(52,211,153,.12)', fill: true, tension: .4, pointRadius: 4, pointHoverRadius: 6, borderWidth: 2.5, pointBackgroundColor: '#34d399' }
      ]
    },
    options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: '#8d97af', font: { size: 12 } } } }, scales: { x: { ticks: { color: '#586077', font: { size: 11 } }, grid: { color: '#232a3d' } }, y: { ticks: { color: '#586077', font: { size: 11 } }, grid: { color: '#232a3d' }, beginAtZero: true } } }
  });
}

// === TRACKING ===
async function loadTracking() {
  var d = await api('/api/tracking');
  if (!d) return;
  var info = document.getElementById('trackingInfo');
  if (d.tracker) {
    var st = d.tracker.status === 'active' ? '✅ Активен' : '⛔ ' + d.tracker.status;
    info.innerHTML =
      '<div class="stat-card"><div class="glow" style="background:var(--green)"></div><div class="num" style="color:var(--green)">'+(d.tracker.last_balance != null ? d.tracker.last_balance : '—')+'</div><div class="label">💎 Баланс</div><div class="sub">'+st+'</div></div>' +
      '<div class="stat-card"><div class="glow" style="background:var(--blue)"></div><div class="num" style="color:var(--blue);font-size:18px;word-break:break-all">'+d.tracker.login+'</div><div class="label">👤 Аккаунт</div><div class="sub">обновлено: '+(d.tracker.last_checked||'—')+'</div></div>';
  } else {
    info.innerHTML = '<div class="empty" style="width:100%">Отслеживающий аккаунт не задан — добавьте токен выше</div>';
  }
  var html = '<div style="padding:20px"><h3 style="font-size:15px;color:var(--text2);margin-bottom:14px;display:flex;align-items:center;gap:8px">📋 Лог трекинга</h3>';
  if (d.log && d.log.length) {
    d.log.forEach(function(l) {
      html += '<div class="log-item"><span class="ltime">'+l.time+'</span><span class="lbadge broadcast">🔍</span><span class="lmsg">'+l.message+'</span></div>';
    });
  } else {
    html += '<div class="empty">Нет записей</div>';
  }
  html += '</div>';
  document.getElementById('trackingLog').innerHTML = html;
}

async function addTracking() {
  var token = document.getElementById('trackingToken').value.trim();
  var statusEl = document.getElementById('trackingStatus');
  if (!token) { statusEl.textContent = '❌ Вставьте токен'; return; }
  statusEl.textContent = '⏳ Проверяю токен...';
  var btn = document.querySelector('#page-tracking .btn-primary');
  btn.disabled = true;
  try {
    var r = await fetch('/api/tracking/add', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({token: token}),
    });
    var d = await r.json();
    if (d.ok) {
      statusEl.textContent = '✅ Добавлен: ' + d.login;
      document.getElementById('trackingToken').value = '';
    } else {
      statusEl.textContent = '❌ ' + (d.error || 'Ошибка');
    }
  } catch(e) {
    statusEl.textContent = '❌ Ошибка соединения';
  }
  btn.disabled = false;
  loadTracking();
}

async function deleteTracking() {
  var statusEl = document.getElementById('trackingStatus');
  statusEl.textContent = '⏳ Удаляю...';
  try {
    await fetch('/api/tracking/delete', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: '{}',
    });
    statusEl.textContent = '✅ Удалён';
  } catch(e) {
    statusEl.textContent = '❌ Ошибка соединения';
  }
  loadTracking();
}

// === BROADCAST ===
async function sendBroadcast() {
  var text = document.getElementById('broadcastText').value.trim();
  var mode = document.getElementById('broadcastMode').value;
  var photo = document.getElementById('broadcastPhoto').value.trim();
  if (!text && !photo) { document.getElementById('broadcastStatus').textContent = '❌ Заполните текст или добавьте фото'; return; }
  document.getElementById('broadcastStatus').textContent = '⏳ Отправка...';
  var btn = document.querySelector('#page-broadcast .btn-primary');
  btn.disabled = true;
  try {
    var r = await fetch('/api/broadcast', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text: text, mode: mode, photo: photo}),
    });
    var d = await r.json();
    var el = document.getElementById('broadcastResult');
    if (d.ok) {
      el.className = 'broadcast-result ok';
      el.innerHTML = '✅ Отправлено: <b>' + d.sent + '</b>, Ошибок: <b>' + d.failed + '</b>';
      document.getElementById('broadcastStatus').textContent = '✅ Готово';
    } else {
      el.className = 'broadcast-result fail';
      el.innerHTML = '❌ ' + (d.error || 'Ошибка');
      document.getElementById('broadcastStatus').textContent = '';
    }
  } catch(e) {
    document.getElementById('broadcastResult').className = 'broadcast-result fail';
    document.getElementById('broadcastResult').innerHTML = '❌ Ошибка соединения';
    document.getElementById('broadcastStatus').textContent = '';
  }
  btn.disabled = false;
}

// === SYSTEM ===
async function loadSystem() {
  var d = await api('/api/stats');
  var slog = await api('/api/dashboard/log');
  if (!d) { document.getElementById('sysStats').innerHTML = '<div class="empty">Ошибка</div>'; return; }
  document.getElementById('sysStats').innerHTML =
    '<div class="stat-card"><div class="glow" style="background:var(--blue)"></div><div class="num" style="color:var(--blue)">'+d.today_users+'</div><div class="label">👥 Новых юзеров сегодня</div></div>' +
    '<div class="stat-card"><div class="glow" style="background:var(--green)"></div><div class="num" style="color:var(--green)">'+d.today_accounts+'</div><div class="label">📦 Новых аккаунтов сегодня</div></div>' +
    '<div class="stat-card"><div class="glow" style="background:var(--yellow)"></div><div class="num" style="color:var(--yellow)">'+d.total_spins+'</div><div class="label">🎡 Всего прокруток</div></div>' +
    '<div class="stat-card"><div class="glow" style="background:var(--cyan)"></div><div class="num" style="color:var(--cyan)">'+d.total_bonuses+'</div><div class="label">🏆 Всего призов</div></div>';
  var html = '<div style="padding:20px"><h3 style="font-size:15px;color:var(--text2);margin-bottom:14px;display:flex;align-items:center;gap:8px">📋 Лог действий</h3>';
  if (slog && slog.length) {
    slog.forEach(function(l) {
      html += '<div class="log-item"><span class="ltime">'+l.time+'</span><span class="lbadge broadcast">⚙️</span><span class="lmsg"><b>'+l.action+'</b> — '+l.detail+'</span></div>';
    });
  } else {
    html += '<div class="empty">Нет записей</div>';
  }
  html += '</div>';
  document.getElementById('sysLog').innerHTML = html;
}

// Init
refreshDashboard();
TIMERS.push(setInterval(refreshDashboard, 15000));
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML_PAGE


@app.on_event("startup")
def startup():
    add_log("system", "startup", "Dashboard started")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    print(f"🌐 Dashboard: http://0.0.0.0:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")

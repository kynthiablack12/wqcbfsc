import json
import os
import threading
from datetime import datetime
from pathlib import Path

import requests
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

import database as db
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


@app.get("/api/stats")
def api_stats():
    conn = db.get_conn()
    users = conn.execute("SELECT COUNT(*) AS v FROM users").fetchone()["v"]
    total_accs = conn.execute("SELECT COUNT(*) AS v FROM accounts").fetchone()["v"]
    active_accs = conn.execute("SELECT COUNT(*) AS v FROM accounts WHERE status='active'").fetchone()["v"]
    active_users_rows = conn.execute("SELECT DISTINCT user_id AS v FROM accounts WHERE status='active'").fetchall()
    active_users = len(active_users_rows)
    total_balance = conn.execute("SELECT COALESCE(SUM(last_balance),0) AS v FROM accounts WHERE status='active'").fetchone()["v"]
    total_spins = conn.execute("SELECT COUNT(*) AS v FROM prizes").fetchone()["v"]
    total_bonuses = conn.execute("SELECT COUNT(*) AS v FROM prizes WHERE prize_title!=''").fetchone()["v"]
    today_str = datetime.now().strftime("%Y-%m-%d")
    today_users = conn.execute("SELECT COUNT(*) AS v FROM users WHERE created_at >= ?", (today_str,)).fetchone()["v"]
    today_accs = conn.execute("SELECT COUNT(*) AS v FROM accounts WHERE created_at >= ?", (today_str,)).fetchone()["v"]
    return {
        "users": users,
        "total_accounts": total_accs,
        "active_accounts": active_accs,
        "active_users": active_users,
        "total_balance": total_balance,
        "total_spins": total_spins,
        "total_bonuses": total_bonuses,
        "today_users": today_users,
        "today_accounts": today_accs,
    }


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
               (SELECT MAX(COALESCE(a.last_spin, a.created_at)) FROM accounts a WHERE a.user_id=u.telegram_id) as last_active
        FROM users u
        WHERE {where_sql}
        ORDER BY total_balance DESC
        LIMIT ? OFFSET ?
    """, params + [limit, offset]).fetchall()

    users_list = []
    for r in rows:
        last_active = r["last_active"]
        if last_active:
            try:
                last_active = datetime.fromisoformat(last_active).strftime("%d.%m %H:%M")
            except Exception:
                pass
        users_list.append({
            "id": r["telegram_id"],
            "username": r["username"] or "",
            "first_name": r["first_name"] or "",
            "created_at": r["created_at"],
            "accounts": r["acc_count"],
            "active_accounts": r["active_count"],
            "total_balance": r["total_balance"],
            "total_spins": r["total_spins"],
            "total_bonuses": r["total_bonuses"],
            "last_active": last_active or "",
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
        "last_spin": (datetime.fromisoformat(r["last_spin"]).strftime("%d.%m %H:%M") if r["last_spin"] else ""),
        "created_at": (datetime.fromisoformat(r["created_at"]).strftime("%d.%m %H:%M") if r["created_at"] else ""),
    } for r in rows]


@app.get("/api/log")
def api_log(limit: int = 50):
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
        "action": "spin",
        "prize": r["action"] or "",
        "balance": r["balance_after"] or 0,
    } for r in rows]


@app.get("/api/top")
def api_top():
    conn = db.get_conn()
    rows = conn.execute("""
        SELECT u.telegram_id, u.username,
               COALESCE((SELECT SUM(last_balance) FROM accounts WHERE user_id=u.telegram_id AND status='active'),0) as balance,
               (SELECT COUNT(*) FROM accounts WHERE user_id=u.telegram_id AND status='active') as accounts
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
    } for r in rows]


@app.post("/api/broadcast")
def api_broadcast(data: dict):
    text = data.get("text", "")
    mode = data.get("mode", "all")
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


HTML_PAGE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Edadeal Bot — Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0c0e14;--surface:#141820;--surface2:#1a1f2b;--border:#232936;--text:#e1e6ef;--text2:#8b95a8;--text3:#545d70;--blue:#4f8aff;--green:#34d399;--yellow:#fbbf24;--purple:#a78bfa;--pink:#f472b6;--red:#f87171}
body{background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Inter','Segoe UI',Roboto,sans-serif;display:flex;min-height:100vh}
.sidebar{width:220px;background:var(--surface);border-right:1px solid var(--border);padding:24px 14px;display:flex;flex-direction:column;position:sticky;top:0;height:100vh}
.logo{font-size:17px;font-weight:700;color:#fff;margin-bottom:28px;display:flex;align-items:center;gap:10px}
.logo .icon{width:30px;height:30px;background:linear-gradient(135deg,var(--blue),var(--purple));border-radius:8px;display:flex;align-items:center;justify-content:center;font-size:14px}
.nav{display:flex;flex-direction:column;gap:2px;flex:1}
.nav a{color:var(--text2);text-decoration:none;padding:9px 12px;border-radius:8px;font-size:13px;font-weight:500;display:flex;align-items:center;gap:9px;transition:.15s;cursor:pointer}
.nav a:hover{background:var(--surface2);color:var(--text)}
.nav a.active{background:linear-gradient(135deg,var(--blue)12,transparent);color:var(--blue)}
.main{flex:1;padding:24px 28px;max-width:1200px}
.header-bar{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px}
.header-bar h1{font-size:20px;font-weight:600}
.btn{padding:7px 14px;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--text);font-size:12px;font-weight:500;cursor:pointer;transition:.15s;display:inline-flex;align-items:center;gap:5px}
.btn:hover{background:var(--surface2);border-color:var(--text3)}
.btn-primary{background:linear-gradient(135deg,var(--blue),#3b6fe0);border:none;color:#fff}
.btn-primary:hover{opacity:.9}
.page{display:none}
.page.active{display:block}
.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:24px}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px 18px;transition:.2s;position:relative;overflow:hidden}
.stat-card:hover{border-color:var(--text3);transform:translateY(-1px)}
.stat-card .num{font-size:26px;font-weight:700;line-height:1.2}
.stat-card .label{font-size:11px;color:var(--text2);margin-top:3px}
.stat-card .glow{position:absolute;top:-50%;right:-30%;width:80px;height:80px;border-radius:50%;opacity:.07;pointer-events:none}
.filters{display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap;align-items:center}
.filters input,.filters select,.filters textarea{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:7px 10px;color:var(--text);font-size:12px;outline:none}
.filters input:focus,.filters select:focus,.filters textarea:focus{border-color:var(--blue)}
.filters input{width:180px}
.filters textarea{width:100%;min-height:100px;resize:vertical;font-family:inherit}
.table-wrap{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:24px}
.table-wrap table{width:100%;border-collapse:collapse;font-size:12px}
.table-wrap th{background:var(--surface2);color:var(--text2);font-weight:600;font-size:10px;text-transform:uppercase;letter-spacing:.4px;text-align:left;padding:10px 14px;border-bottom:1px solid var(--border)}
.table-wrap td{padding:10px 14px;border-bottom:1px solid var(--border);vertical-align:middle}
.table-wrap tr:last-child td{border-bottom:0}
.table-wrap tr:hover td{background:rgba(79,138,255,.03)}
.status-dot{display:inline-block;width:6px;height:6px;border-radius:50%;margin-right:5px}
.status-dot.on{background:var(--green);box-shadow:0 0 6px var(--green)44}
.status-dot.off{background:var(--text3)}
.expand-btn{cursor:pointer;color:var(--text3);transition:transform .2s;font-size:11px;user-select:none;display:inline-block;width:16px}
.expand-btn.open{transform:rotate(90deg)}
.sub-row{display:none}
.sub-row.open{display:table-row}
.sub-table td{padding:6px 14px 6px 42px;font-size:11px;border-bottom:1px solid var(--border)}
.sub-table .masked{color:var(--text3);font-family:monospace;letter-spacing:2px;font-size:11px}
.sub-table .bal{color:var(--yellow);font-weight:600}
.sub-table .st{font-size:10px}
.sub-table .st.active{color:var(--green)}
.sub-table .st.expired{color:var(--red)}
.pagination{display:flex;justify-content:space-between;align-items:center;padding:10px 14px;border-top:1px solid var(--border);font-size:12px;color:var(--text2)}
.pagination .pages{display:flex;gap:3px}
.pagination .pages span{width:26px;height:26px;display:flex;align-items:center;justify-content:center;border-radius:5px;cursor:pointer;font-size:12px;transition:.15s}
.pagination .pages span:hover{background:var(--surface2)}
.pagination .pages span.active{background:var(--blue);color:#fff}
.bottom-grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
.section-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.section-header h2{font-size:14px;font-weight:600;display:flex;align-items:center;gap:7px}
.log-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.log-item{display:flex;align-items:center;gap:8px;padding:8px 14px;border-bottom:1px solid var(--border);font-size:12px}
.log-item:last-child{border-bottom:0}
.log-item .ltime{color:var(--text3);font-family:monospace;font-size:10px;min-width:60px;white-space:nowrap}
.log-item .lbadge{font-size:9px;font-weight:600;padding:2px 7px;border-radius:10px;white-space:nowrap}
.log-item .lbadge.spin{background:var(--yellow)15;color:var(--yellow)}
.log-item .lbadge.bonus{background:var(--green)15;color:var(--green)}
.log-item .lbadge.broadcast{background:var(--blue)15;color:var(--blue)}
.log-item .lmsg{flex:1;color:var(--text2)}
.log-item .lmsg b{color:var(--text)}
.log-item .lmsg .m{color:var(--text3);font-family:monospace;letter-spacing:1px}
.leaderboard{list-style:none}
.leaderboard li{display:flex;align-items:center;padding:8px 14px;border-bottom:1px solid var(--border);gap:10px;font-size:12px}
.leaderboard li:last-child{border-bottom:0}
.leaderboard .pos{width:20px;height:20px;border-radius:5px;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;background:var(--surface2);color:var(--text2)}
.leaderboard .pos.gold{background:var(--yellow)18;color:var(--yellow)}
.leaderboard .pos.silver{background:#c0c0c018;color:#c0c0c0}
.leaderboard .pos.bronze{background:#cd7f3218;color:#cd7f32}
.leaderboard .lname{flex:1;color:var(--text)}
.leaderboard .lscore{color:var(--yellow);font-weight:600}
.broadcast-box{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:20px;max-width:600px}
.broadcast-box label{display:block;font-size:12px;color:var(--text2);margin-bottom:4px;margin-top:12px}
.broadcast-box label:first-child{margin-top:0}
.broadcast-result{margin-top:12px;padding:10px 14px;border-radius:8px;font-size:12px;display:none}
.broadcast-result.ok{display:block;background:var(--green)12;color:var(--green);border:1px solid var(--green)22}
.broadcast-result.fail{display:block;background:var(--red)12;color:var(--red);border:1px solid var(--red)22}
.user-info .name{font-weight:500}
.user-info .sub{font-size:10px;color:var(--text3);font-family:monospace}
.loading{opacity:.4;pointer-events:none}
td .bal-val{color:var(--yellow);font-weight:600}
.empty{text-align:center;padding:30px;color:var(--text3);font-size:13px}

/* Animations */
@keyframes fadeSlide{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes pulseGlow{0%,100%{opacity:.08}50%{opacity:.15}}
@keyframes countUp{from{opacity:0;transform:scale(.7)}to{opacity:1;transform:scale(1)}}
.stat-card{animation:fadeSlide .4s ease both}
.stat-card:nth-child(1){animation-delay:0s}
.stat-card:nth-child(2){animation-delay:.05s}
.stat-card:nth-child(3){animation-delay:.1s}
.stat-card:nth-child(4){animation-delay:.15s}
.stat-card:nth-child(5){animation-delay:.2s}
.table-wrap{animation:fadeIn .3s ease}
.log-item{animation:fadeSlide .3s ease both}
.log-item:nth-child(1){animation-delay:0s}
.log-item:nth-child(2){animation-delay:.03s}
.log-item:nth-child(3){animation-delay:.06s}
.log-item:nth-child(4){animation-delay:.09s}
.log-item:nth-child(5){animation-delay:.12s}
.log-item:nth-child(6){animation-delay:.15s}
.log-item:nth-child(7){animation-delay:.18s}
.log-item:nth-child(8){animation-delay:.21s}
.leaderboard li{animation:fadeSlide .3s ease both}
.leaderboard li:nth-child(1){animation-delay:0s}
.leaderboard li:nth-child(2){animation-delay:.04s}
.leaderboard li:nth-child(3){animation-delay:.08s}
.leaderboard li:nth-child(4){animation-delay:.12s}
.leaderboard li:nth-child(5){animation-delay:.16s}
.stat-card .glow{animation:pulseGlow 3s ease-in-out infinite}
.page{transition:opacity .25s ease}
.broadcast-box{animation:fadeSlide .35s ease}
#stats{transition:opacity .2s ease}
.log-card,#topList{transition:opacity .2s ease}
.stat-card .num{transition:color .3s ease}

@media(max-width:768px){
  .sidebar{display:none}
  .main{padding:16px}
  .stats{grid-template-columns:repeat(2,1fr)}
  .bottom-grid{grid-template-columns:1fr}
}
</style>
</head>
<body>
<aside class="sidebar">
  <div class="logo"><div class="icon">🎡</div> Edadeal Bot</div>
  <div class="nav">
    <a class="active" data-page="dashboard">📊 Дашборд</a>
    <a data-page="users">👥 Пользователи</a>
    <a data-page="broadcast">📨 Рассылка</a>
    <a data-page="system">📡 Система</a>
  </div>
  <div style="padding-top:12px;border-top:1px solid var(--border);margin-top:auto;font-size:11px;color:var(--text3)">
    v2.0 · обновление каждые 15с
  </div>
</aside>

<div class="main" id="app">
  <!-- DASHBOARD -->
  <div class="page active" id="page-dashboard">
    <div class="header-bar">
      <h1>📊 Дашборд</h1>
      <button class="btn" onclick="refresh()">🔄 Обновить</button>
    </div>
    <div class="stats" id="stats"></div>
    <div class="bottom-grid" style="margin-top:0">
      <div>
        <div class="section-header"><h2>📜 Активность</h2></div>
        <div class="log-card" id="logList"><div class="empty">Загрузка...</div></div>
      </div>
      <div>
        <div class="section-header"><h2>🏆 Топ юзеров</h2><span style="font-size:11px;color:var(--text2)">по алмазам</span></div>
        <div class="log-card" id="topList"><div class="empty">Загрузка...</div></div>
      </div>
    </div>
  </div>

  <!-- USERS -->
  <div class="page" id="page-users">
    <div class="header-bar"><h1>👥 Пользователи</h1></div>
    <div class="filters">
      <input type="text" id="search" placeholder="🔍 Поиск по username или ID..." oninput="loadUsers()">
      <select id="statusFilter" onchange="loadUsers()">
        <option value="">Все статусы</option>
        <option value="active">Только активные</option>
        <option value="inactive">Неактивные</option>
      </select>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th style="width:16px"></th>
          <th>Пользователь</th>
          <th>Аккаунты</th>
          <th>Актив</th>
          <th>💎 Алмазы</th>
          <th>🎡 Прокрутки</th>
          <th>🏆 Бонусы</th>
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

  <!-- BROADCAST -->
  <div class="page" id="page-broadcast">
    <div class="header-bar"><h1>📨 Рассылка</h1></div>
    <div class="broadcast-box">
      <label>Кому отправляем</label>
      <select id="broadcastMode" class="filters" style="margin-bottom:12px;padding:7px 10px;background:var(--surface);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:12px;width:auto">
        <option value="all">Всем пользователям</option>
        <option value="active">Только активным</option>
      </select>
      <label>Текст сообщения (HTML)</label>
      <textarea id="broadcastText" style="width:100%;min-height:120px;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:10px;color:var(--text);font-size:13px;resize:vertical;font-family:inherit" placeholder="Напишите сообщение..."></textarea>
      <div style="margin-top:12px;display:flex;gap:8px;align-items:center">
        <button class="btn btn-primary" onclick="sendBroadcast()">📨 Отправить</button>
        <span id="broadcastStatus" style="font-size:12px;color:var(--text2)"></span>
      </div>
      <div class="broadcast-result" id="broadcastResult"></div>
    </div>
  </div>

  <!-- SYSTEM -->
  <div class="page" id="page-system">
    <div class="header-bar"><h1>📡 Система</h1></div>
    <div class="broadcast-box" id="systemInfo">
      <p style="color:var(--text3);font-size:13px">Загрузка...</p>
    </div>
  </div>
</div>

<script>
var currentPage = 1;
var TIMERS = [];

function switchPage(name) {
  document.querySelectorAll('.page').forEach(function(p) { p.classList.remove('active'); });
  document.getElementById('page-' + name).classList.add('active');
  document.querySelectorAll('.nav a').forEach(function(a) { a.classList.remove('active'); });
  document.querySelector('.nav a[data-page="' + name + '"]').classList.add('active');
  TIMERS.forEach(function(t) { clearInterval(t); });
  TIMERS = [];
  if (name === 'dashboard') { refresh(); TIMERS.push(setInterval(refresh, 15000)); }
  if (name === 'users') { loadUsers(); }
  if (name === 'system') { loadSystem(); }
}

document.querySelectorAll('.nav a').forEach(function(a) {
  a.onclick = function() { switchPage(this.dataset.page); };
});

async function api(path) {
  try { var r = await fetch(path); return await r.json(); }
  catch(e) { return null; }
}

// === DASHBOARD ===
async function loadStats() {
  var d = await api('/api/stats');
  if (!d) return;
  document.getElementById('stats').innerHTML =
    '<div class="stat-card"><div class="glow"></div><div class="num" style="color:var(--blue)">'+d.users+'</div><div class="label">Пользователей</div></div>' +
    '<div class="stat-card"><div class="glow"></div><div class="num" style="color:var(--green)">'+d.total_accounts+'</div><div class="label">Всего аккаунтов</div></div>' +
    '<div class="stat-card"><div class="glow"></div><div class="num" style="color:var(--yellow)">'+d.active_accounts+'</div><div class="label">Активных аккаунтов</div></div>' +
    '<div class="stat-card"><div class="glow"></div><div class="num" style="color:var(--purple)">'+d.active_users+'</div><div class="label">Активных юзеров</div></div>' +
    '<div class="stat-card"><div class="glow"></div><div class="num" style="color:var(--pink)">'+d.total_balance.toLocaleString()+' <span style="font-size:14px">💎</span></div><div class="label">Всего алмазов</div></div>';
}

async function loadLog() {
  var d = await api('/api/log?limit=15');
  if (!d) return;
  var el = document.getElementById('logList');
  if (!d.length) { el.innerHTML = '<div class="empty">Нет записей</div>'; return; }
  el.innerHTML = d.map(function(l) {
    var badge = l.prize ? 'bonus' : 'spin';
    var icon = l.prize ? '🎯' : '🎡';
    var label = l.prize ? l.prize.substring(0,20) : 'spin';
    return '<div class="log-item"><span class="ltime">'+l.time+'</span>' +
      '<span class="lbadge '+badge+'">'+icon+'</span>' +
      '<span class="lmsg"><b>ID:'+l.user_id+'</b> <span class="m">'+l.login_masked+'</span> — '+label+'</span></div>';
  }).join('');
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
      '<span class="lscore">' + u.balance.toLocaleString() + ' 💎</span></li>';
  }).join('') + '</ol>';
}

async function refresh() {
  currentPage = 1;
  await Promise.all([loadStats(), loadLog(), loadTop()]);
}

// === USERS ===
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
    return;
  }
  d.users.forEach(function(u) {
    var tr = document.createElement('tr');
    var activeDot = u.active_accounts > 0 ? 'on' : 'off';
    var username = u.username ? '@' + u.username : 'ID: ' + u.id;
    tr.innerHTML = '<td><span class="expand-btn" data-uid="'+u.id+'">▶</span></td>' +
      '<td><div class="user-info"><span class="name"><span class="status-dot '+activeDot+'"></span>' +
      username + '</span><span class="sub">ID: ' + u.id + ' · '+u.active_accounts+' активных</span></div></td>' +
      '<td>'+u.accounts+'</td><td>'+u.active_accounts+'</td>' +
      '<td class="bal-val">'+u.total_balance.toLocaleString()+'</td>' +
      '<td>'+u.total_spins+'</td><td style="color:var(--green)">'+u.total_bonuses+'</td>' +
      '<td style="font-size:11px;color:var(--text2)">'+(u.last_active||'—')+'</td>';
    tbody.appendChild(tr);
  });
  document.getElementById('paginationInfo').textContent = 'Показано '+d.users.length+' из '+d.total;
  var pp = document.getElementById('paginationPages');
  pp.innerHTML = '';
  for (var i = 1; i <= d.pages && i <= 10; i++) {
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
          '<td class="bal">'+a.balance+' 💎</td>' +
          '<td style="font-size:10px;color:var(--text2)">'+(a.last_spin||'—')+'</td></tr>';
      }).join('') +
      '</table></td>';
    row.after(tr);
  });
});

// === BROADCAST ===
async function sendBroadcast() {
  var text = document.getElementById('broadcastText').value.trim();
  var mode = document.getElementById('broadcastMode').value;
  if (!text) { document.getElementById('broadcastStatus').textContent = '❌ Введите текст'; return; }
  document.getElementById('broadcastStatus').textContent = '⏳ Отправка...';
  var btn = document.querySelector('#page-broadcast .btn-primary');
  btn.disabled = true;
  try {
    var r = await fetch('/api/broadcast', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({text: text, mode: mode}),
    });
    var d = await r.json();
    var el = document.getElementById('broadcastResult');
    if (d.ok) {
      el.className = 'broadcast-result ok';
      el.innerHTML = '✅ Отправлено: ' + d.sent + ', Ошибок: ' + d.failed;
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
  var el = document.getElementById('systemInfo');
  var d = await api('/api/stats');
  var slog = await api('/api/dashboard/log');
  if (!d) { el.innerHTML = '<p style="color:var(--text3)">Ошибка загрузки</p>'; return; }
  var html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px">';
  html += '<div><span style="color:var(--text2);font-size:12px">Пользователей сегодня</span><br><span style="font-size:20px;font-weight:700">'+d.today_users+'</span></div>';
  html += '<div><span style="color:var(--text2);font-size:12px">Аккаунтов сегодня</span><br><span style="font-size:20px;font-weight:700">'+d.today_accounts+'</span></div>';
  html += '<div><span style="color:var(--text2);font-size:12px">Всего прокруток</span><br><span style="font-size:20px;font-weight:700">'+d.total_spins+'</span></div>';
  html += '<div><span style="color:var(--text2);font-size:12px">Всего призов</span><br><span style="font-size:20px;font-weight:700">'+d.total_bonuses+'</span></div>';
  html += '</div>';
  html += '<div class="section-header"><h2>📋 Лог действий</h2></div>';
  html += '<div class="log-card">';
  if (slog && slog.length) {
    slog.forEach(function(l) {
      html += '<div class="log-item"><span class="ltime">'+l.time+'</span><span class="lbadge broadcast">⚙️</span><span class="lmsg"><b>'+l.action+'</b> — '+l.detail+'</span></div>';
    });
  } else {
    html += '<div class="empty">Нет записей</div>';
  }
  html += '</div>';
  el.innerHTML = html;
}

// Init
refresh();
TIMERS.push(setInterval(refresh, 15000));
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

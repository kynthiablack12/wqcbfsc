from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
import uvicorn

import database as db

BASE = Path(__file__).parent
app = FastAPI(title="Edadeal Bot Dashboard")


@app.get("/api/stats")
def api_stats():
    conn = db.get_conn()
    users = conn.execute("SELECT COUNT(*) AS v FROM users").fetchone()["v"]
    total_accs = conn.execute("SELECT COUNT(*) AS v FROM accounts").fetchone()["v"]
    active_accs = conn.execute("SELECT COUNT(*) AS v FROM accounts WHERE status='active'").fetchone()["v"]
    active_users = conn.execute("SELECT COUNT(DISTINCT user_id) AS v FROM accounts WHERE status='active'").fetchone()["v"]
    total_balance = conn.execute("SELECT COALESCE(SUM(last_balance),0) AS v FROM accounts WHERE status='active'").fetchone()["v"]
    total_spins = conn.execute("SELECT COUNT(*) AS v FROM prizes").fetchone()["v"]
    total_bonuses = conn.execute("SELECT COUNT(*) AS v FROM prizes WHERE prize_title!=''").fetchone()["v"]
    today = datetime.now().strftime("%Y-%m-%d")
    today_users = conn.execute("SELECT COUNT(*) AS v FROM users WHERE created_at >= ?", (today,)).fetchone()["v"]
    today_accs = conn.execute("SELECT COUNT(*) AS v FROM accounts WHERE created_at >= ?", (today,)).fetchone()["v"]
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
    elif status == "banned":
        where.append("u.telegram_id IN (SELECT user_id FROM accounts WHERE status='expired')")

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
               COALESCE((SELECT SUM(last_balance) FROM accounts WHERE user_id=u.telegram_id AND status='active'),0) as balance
        FROM users u
        GROUP BY u.telegram_id
        HAVING balance > 0
        ORDER BY balance DESC
        LIMIT 10
    """).fetchall()
    return [{
        "id": r["telegram_id"],
        "username": (r["username"] or f"ID:{r['telegram_id']}"),
        "balance": r["balance"],
    } for r in rows]


HTML = (BASE / "dashboard_mockup.html").read_text(encoding="utf-8") if (BASE / "dashboard_mockup.html").exists() else ""


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


if __name__ == "__main__":
    print("🌐 Dashboard: http://127.0.0.1:5050")
    uvicorn.run(app, host="127.0.0.1", port=5050, log_level="info")

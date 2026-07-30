import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

import requests
from fastapi import FastAPI, Form, Query
from fastapi.responses import HTMLResponse, JSONResponse
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
    active_users = conn.execute("SELECT DISTINCT user_id AS v FROM accounts WHERE status='active'").fetchall()
    active_users_count = len(active_users)
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
        "active_users": active_users_count,
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
        "login": r["login"][:2] + "•••" + r["login"][-1:] if len(r["login"]) > 3 else "•••",
        "status": r["status"],
        "balance": r["last_balance"] or 0,
        "last_spin": (datetime.fromisoformat(r["last_spin"]).strftime("%d.%m %H:%M") if r["last_spin"] else ""),
        "created_at": (datetime.fromisoformat(r["created_at"]).strftime("%d.%m %H:%M") if r["created_at"] else ""),
    } for r in rows]


@app.get("/api/log")
def api_log(limit: int = 50):
    with _log_lock:
        return _log_buffer[:limit]


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
def api_broadcast(text: str = Form(...), mode: str = Form("all")):
    if not TG_BOT_TOKEN:
        return {"ok": False, "error": "TG_BOT_TOKEN not set"}

    conn = db.get_conn()
    if mode == "all":
        rows = conn.execute("SELECT telegram_id FROM users").fetchall()
    elif mode == "active":
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
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>Edadeal Bot Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f0f1a;color:#e0e0e0;min-height:100vh}
.nav{display:flex;overflow-x:auto;background:#1a1a2e;border-bottom:1px solid #2a2a4a;position:sticky;top:0;z-index:10;-webkit-overflow-scrolling:touch}
.nav::-webkit-scrollbar{display:none}
.nav a{flex-shrink:0;padding:14px 18px;text-decoration:none;color:#888;font-size:14px;font-weight:500;border-bottom:2px solid transparent;transition:all .2s}
.nav a.active{color:#7c5cfc;border-bottom-color:#7c5cfc}
.nav a:hover{color:#e0e0e0}
.content{padding:16px;max-width:1000px;margin:0 auto}
.tab{display:none}
.tab.active{display:block}
.stats-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-bottom:20px}
.stat-card{background:#1a1a2e;border-radius:12px;padding:16px;text-align:center;border:1px solid #2a2a4a}
.stat-card .val{font-size:24px;font-weight:700;color:#7c5cfc}
.stat-card .lbl{font-size:12px;color:#888;margin-top:4px}
h2{font-size:18px;margin:16px 0;color:#ccc}
table{width:100%;border-collapse:collapse;font-size:13px;background:#1a1a2e;border-radius:10px;overflow:hidden;border:1px solid #2a2a4a}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #2a2a4a}
th{background:#16162a;color:#888;font-weight:600;font-size:11px;text-transform:uppercase}
tr:hover{background:#22223a}
tr.status-active{color:#4caf50}
tr.status-expired{color:#f44336}
.pagination{display:flex;gap:8px;justify-content:center;margin:16px 0;flex-wrap:wrap}
.pagination button{background:#2a2a4a;border:1px solid #3a3a5a;color:#e0e0e0;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:13px}
.pagination button:hover{background:#3a3a5a}
.pagination button.active{background:#7c5cfc;border-color:#7c5cfc}
input,select,textarea{background:#1a1a2e;border:1px solid #3a3a5a;color:#e0e0e0;padding:10px 14px;border-radius:10px;font-size:14px;width:100%;margin-bottom:10px}
input:focus,select:focus,textarea:focus{outline:none;border-color:#7c5cfc}
textarea{resize:vertical;min-height:120px;font-family:inherit}
label{display:block;font-size:13px;color:#888;margin-bottom:4px}
.btn{background:#7c5cfc;border:none;color:#fff;padding:12px 24px;border-radius:10px;font-size:15px;font-weight:600;cursor:pointer;transition:all .2s}
.btn:hover{background:#6a4ae8}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-sm{background:#2a2a4a;border:1px solid #3a3a5a;color:#e0e0e0;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:12px}
.btn-sm:hover{background:#3a3a5a}
.search-bar{display:flex;gap:8px;margin-bottom:12px}
.search-bar input{flex:1;margin-bottom:0}
.search-bar select{width:auto;flex-shrink:0;margin-bottom:0}
.search-bar button{flex-shrink:0;margin-bottom:0}
.mt-4{margin-top:16px}
.mb-4{margin-bottom:16px}
.text-center{text-align:center}
.text-green{color:#4caf50}
.text-red{color:#f44336}
.text-muted{color:#888;font-size:12px}
.tag{display:inline-block;padding:2px 8px;border-radius:6px;font-size:11px;font-weight:600}
.tag.active{background:#1b3a1b;color:#4caf50}
.tag.expired{background:#3a1b1b;color:#f44336}
.modal{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:20;align-items:center;justify-content:center}
.modal.show{display:flex}
.modal-content{background:#1a1a2e;border-radius:14px;padding:24px;max-width:500px;width:90%;max-height:80vh;overflow-y:auto;border:1px solid #2a2a4a}
.modal-content h3{margin-bottom:12px}
.accounts-modal{font-size:13px}
.accounts-modal td{padding:8px 10px}
@keyframes pulse{0%{opacity:1}50%{opacity:.4}100%{opacity:1}}
.loading{animation:pulse 1.2s infinite}
.notification{position:fixed;top:16px;right:16px;background:#1a1a2e;border:1px solid #4caf50;border-radius:10px;padding:14px 20px;font-size:14px;z-index:30;transform:translateX(120%);transition:transform .3s;max-width:320px}
.notification.show{transform:translateX(0)}
.notification.error{border-color:#f44336}
.broadcast-result{margin-top:12px;padding:12px;border-radius:10px;font-size:14px}
.broadcast-result.ok{background:#1b3a1b;color:#4caf50}
.broadcast-result.fail{background:#3a1b1b;color:#f44336}
@media(max-width:600px){
  .stats-grid{grid-template-columns:repeat(2,1fr)}
  table{font-size:12px}
  th,td{padding:8px 6px}
  .nav a{padding:12px 14px;font-size:13px}
  .content{padding:12px}
  .search-bar{flex-wrap:wrap}
  .search-bar select,.search-bar button{width:100%}
}
</style>
</head>
<body>

<nav class="nav" id="nav">
  <a href="#" class="active" data-tab="overview">📊 Обзор</a>
  <a href="#" data-tab="users">👥 Пользователи</a>
  <a href="#" data-tab="broadcast">📨 Рассылка</a>
  <a href="#" data-tab="log">📋 Лог</a>
  <a href="#" data-tab="top">🏆 Топ</a>
</nav>

<div class="content" id="content">
  <div class="tab active" id="tab-overview"></div>
  <div class="tab" id="tab-users"></div>
  <div class="tab" id="tab-broadcast"></div>
  <div class="tab" id="tab-log"></div>
  <div class="tab" id="tab-top"></div>
</div>

<div class="modal" id="accountsModal"><div class="modal-content" id="accountsModalBody"></div></div>
<div class="notification" id="notification"></div>

<script>
const TABS = ['overview','users','broadcast','log','top'];
let currentTab = 'overview';

// Navigation
document.querySelectorAll('.nav a').forEach(a => {
  a.onclick = e => {
    e.preventDefault();
    const tab = a.dataset.tab;
    switchTab(tab);
  };
});

function switchTab(tab) {
  currentTab = tab;
  document.querySelectorAll('.nav a').forEach(a => a.classList.toggle('active', a.dataset.tab === tab));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  renderTab(tab);
}

// Notify
function notify(msg, isError=false) {
  const el = document.getElementById('notification');
  el.textContent = msg;
  el.className = 'notification' + (isError ? ' error' : '');
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 3000);
}

// API helper
async function api(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(r.statusText);
  return r.json();
}

// Render
function renderTab(tab) {
  if (tab === 'overview') renderOverview();
  else if (tab === 'users') renderUsers();
  else if (tab === 'broadcast') renderBroadcast();
  else if (tab === 'log') renderLog();
  else if (tab === 'top') renderTop();
}

// ===== OVERVIEW =====
async function renderOverview() {
  const el = document.getElementById('tab-overview');
  el.innerHTML = '<div class="loading text-center" style="padding:40px">Загрузка...</div>';
  try {
    const s = await api('/api/stats');
    el.innerHTML = `
      <div class="stats-grid">
        <div class="stat-card"><div class="val">${s.users}</div><div class="lbl">Пользователей</div></div>
        <div class="stat-card"><div class="val">${s.active_users}</div><div class="lbl">Активных юзеров</div></div>
        <div class="stat-card"><div class="val">${s.total_accounts}</div><div class="lbl">Всего аккаунтов</div></div>
        <div class="stat-card"><div class="val">${s.active_accounts}</div><div class="lbl">Активных</div></div>
        <div class="stat-card"><div class="val">${s.total_balance.toLocaleString()}</div><div class="lbl">💎 Баланс</div></div>
        <div class="stat-card"><div class="val">${s.total_spins}</div><div class="lbl">Прокруток</div></div>
        <div class="stat-card"><div class="val">${s.total_bonuses}</div><div class="lbl">Призов</div></div>
        <div class="stat-card"><div class="val">${s.today_users}</div><div class="lbl">Новых сегодня</div></div>
        <div class="stat-card"><div class="val">${s.today_accounts}</div><div class="lbl">Аккаунтов сегодня</div></div>
      </div>
      <p class="text-muted text-center">Данные за сегодня: ${new Date().toLocaleDateString('ru-RU')}</p>
      <p class="text-muted text-center">Нажми на пользователя чтобы увидеть его аккаунты</p>
    `;
  } catch(e) {
    el.innerHTML = '<div class="text-center text-red">Ошибка загрузки</div>';
  }
}

// ===== USERS =====
let userState = {page:1, search:'', status:''};

async function renderUsers() {
  const {page, search, status} = userState;
  const el = document.getElementById('tab-users');
  el.innerHTML = `
    <div class="search-bar">
      <input type="text" id="userSearch" placeholder="Поиск по username или ID..." value="${search}">
      <select id="userFilter">
        <option value="">Все</option>
        <option value="active" ${status==='active'?'selected':''}>Активные</option>
        <option value="inactive" ${status==='inactive'?'selected':''}>Неактивные</option>
      </select>
      <button class="btn-sm" onclick="userState.search=document.getElementById('userSearch').value;userState.status=document.getElementById('userFilter').value;userState.page=1;renderUsers()">🔍</button>
    </div>
    <div id="usersTable">Загрузка...</div>
    <div class="pagination" id="usersPagination"></div>
  `;
  try {
    const d = await api(`/api/users?page=${page}&limit=20&search=${encodeURIComponent(search)}&status=${status}`);
    let html = '<table><thead><tr><th>ID</th><th>Username</th><th>Акк</th><th>Актив</th><th>💎</th><th>Спин</th><th>Призы</th><th></th></tr></thead><tbody>';
    for (const u of d.users) {
      html += `<tr style="cursor:pointer" onclick="showAccounts(${u.id})">
        <td>${u.id}</td>
        <td>${u.first_name || ''} ${u.username ? '@'+u.username : ''}</td>
        <td>${u.accounts}</td>
        <td><span class="tag ${u.active_accounts > 0 ? 'active' : 'expired'}">${u.active_accounts}</span></td>
        <td>${u.total_balance.toLocaleString()}</td>
        <td>${u.total_spins}</td>
        <td>${u.total_bonuses}</td>
        <td class="text-muted">${u.last_active}</td>
      </tr>`;
    }
    html += '</tbody></table>';
    if (!d.users.length) html = '<p class="text-center text-muted">Ничего не найдено</p>';
    document.getElementById('usersTable').innerHTML = html;

    let phtml = '';
    for (let i=1; i<=d.pages; i++) {
      phtml += `<button class="${i===d.page?'active':''}" onclick="userState.page=${i};renderUsers()">${i}</button>`;
    }
    document.getElementById('usersPagination').innerHTML = phtml;
  } catch(e) {
    document.getElementById('usersTable').innerHTML = '<div class="text-center text-red">Ошибка загрузки</div>';
  }
}

async function showAccounts(userId) {
  const modal = document.getElementById('accountsModal');
  const body = document.getElementById('accountsModalBody');
  body.innerHTML = '<div class="loading">Загрузка...</div>';
  modal.classList.add('show');
  try {
    const accs = await api(`/api/users/${userId}/accounts`);
    let html = `<h3>Аккаунты пользователя #${userId}</h3><button class="btn-sm" onclick="document.getElementById('accountsModal').classList.remove('show')" style="float:right">✕</button>`;
    if (!accs.length) {
      html += '<p class="text-muted">Нет аккаунтов</p>';
    } else {
      html += '<table class="accounts-modal"><thead><tr><th>Логин</th><th>Статус</th><th>💎</th><th>Последний спин</th><th>Создан</th></tr></thead><tbody>';
      for (const a of accs) {
        html += `<tr><td>${a.login}</td><td><span class="tag ${a.status}">${a.status}</span></td><td>${a.balance.toLocaleString()}</td><td class="text-muted">${a.last_spin||'-'}</td><td class="text-muted">${a.created_at||'-'}</td></tr>`;
      }
      html += '</tbody></table>';
    }
    body.innerHTML = html;
  } catch(e) {
    body.innerHTML = '<div class="text-center text-red">Ошибка</div>';
  }
}
// Close modal on backdrop click
document.getElementById('accountsModal').onclick = function(e) {
  if (e.target === this) this.classList.remove('show');
};

// ===== BROADCAST =====
function renderBroadcast() {
  const el = document.getElementById('tab-broadcast');
  el.innerHTML = `
    <h2>📨 Рассылка пользователям</h2>
    <div style="background:#1a1a2e;border-radius:12px;padding:20px;border:1px solid #2a2a4a">
      <label>Кому отправляем</label>
      <select id="broadcastMode">
        <option value="all">Всем пользователям</option>
        <option value="active">Только активным</option>
      </select>
      <label>Текст сообщения (поддерживается HTML)</label>
      <textarea id="broadcastText" placeholder="Напишите сообщение..."></textarea>
      <button class="btn" onclick="sendBroadcast()" id="broadcastBtn">📨 Отправить рассылку</button>
      <div id="broadcastResult"></div>
    </div>
    <p class="text-muted mt-4">Сообщение отправляется через Telegram Bot API.<br>Используйте HTML-теги: &lt;b&gt;, &lt;i&gt;, &lt;code&gt;, &lt;a href="..."&gt;</p>
  `;
}

async function sendBroadcast() {
  const text = document.getElementById('broadcastText').value.trim();
  const mode = document.getElementById('broadcastMode').value;
  if (!text) { notify('Введите текст сообщения', true); return; }

  const btn = document.getElementById('broadcastBtn');
  btn.disabled = true;
  btn.textContent = '⏳ Отправка...';
  document.getElementById('broadcastResult').innerHTML = '';

  try {
    const r = await fetch('/api/broadcast', {
      method: 'POST',
      headers: {'Content-Type':'application/x-www-form-urlencoded'},
      body: 'text=' + encodeURIComponent(text) + '&mode=' + encodeURIComponent(mode),
    });
    const d = await r.json();
    if (d.ok) {
      document.getElementById('broadcastResult').innerHTML = `<div class="broadcast-result ok">✅ Отправлено: ${d.sent}, Ошибок: ${d.failed}</div>`;
      notify(`Рассылка завершена: ${d.sent} получено, ${d.failed} ошибок`);
    } else {
      document.getElementById('broadcastResult').innerHTML = `<div class="broadcast-result fail">❌ ${d.error}</div>`;
    }
  } catch(e) {
    document.getElementById('broadcastResult').innerHTML = `<div class="broadcast-result fail">❌ Ошибка соединения</div>`;
  }
  btn.disabled = false;
  btn.textContent = '📨 Отправить рассылку';
}

// ===== LOG =====
async function renderLog() {
  const el = document.getElementById('tab-log');
  el.innerHTML = '<div class="loading text-center" style="padding:40px">Загрузка...</div>';
  try {
    const items = await api('/api/dashboard/log');
    if (!items.length) {
      el.innerHTML = '<p class="text-center text-muted">Пока нет событий</p>';
      return;
    }
    let html = '<table><thead><tr><th>Время</th><th>Пользователь</th><th>Действие</th><th>Детали</th></tr></thead><tbody>';
    for (const item of items) {
      html += `<tr><td class="text-muted">${item.time}</td><td>${item.user_id}</td><td>${item.action}</td><td class="text-muted">${item.detail}</td></tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<div class="text-center text-red">Ошибка загрузки</div>';
  }
}

// ===== TOP =====
async function renderTop() {
  const el = document.getElementById('tab-top');
  el.innerHTML = '<div class="loading text-center" style="padding:40px">Загрузка...</div>';
  try {
    const items = await api('/api/top');
    let html = '<table><thead><tr><th>#</th><th>Пользователь</th><th>Аккаунтов</th><th>💎 Баланс</th></tr></thead><tbody>';
    for (let i=0; i<items.length; i++) {
      const medal = i===0?'🥇':i===1?'🥈':i===2?'🥉':(i+1);
      html += `<tr><td>${medal}</td><td>${items[i].username}</td><td>${items[i].accounts}</td><td>${items[i].balance.toLocaleString()}</td></tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<div class="text-center text-red">Ошибка загрузки</div>';
  }
}

// Real-time polling (every 5s on log tab)
setInterval(async () => {
  if (currentTab === 'overview') renderOverview();
  if (currentTab === 'log') renderLog();
  if (currentTab === 'top') renderTop();
}, 5000);

// Initial render
renderOverview();
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

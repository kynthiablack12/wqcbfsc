import os
import re
import threading
from datetime import datetime

try:
    import sqlite3
except ImportError:
    sqlite3 = None
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    psycopg2 = None

_local = threading.local()


def _is_pg():
    return bool(os.environ.get("DATABASE_URL"))


def _q(sql):
    if _is_pg():
        return re.sub(r"\?", "%s", sql)
    return sql


class _Conn:
    """Unified connection wrapper — sqlite3 or psycopg2."""
    def __init__(self):
        if _is_pg():
            self._raw = psycopg2.connect(os.environ["DATABASE_URL"])
            self._raw.autocommit = True
            self._raw.cursor_factory = psycopg2.extras.RealDictCursor
            self._backend = "pg"
        else:
            from config import DB_PATH
            self._raw = sqlite3.connect(DB_PATH, check_same_thread=False)
            self._raw.row_factory = sqlite3.Row
            self._raw.execute("PRAGMA journal_mode=WAL")
            self._backend = "lite"

    def execute(self, sql, params=None):
        sql = _q(sql)
        if self._backend == "pg":
            cur = self._raw.cursor()
            cur.execute(sql, params or ())
            return cur
        return self._raw.execute(sql, params or ())

    def commit(self):
        if self._backend == "lite":
            self._raw.commit()

    def executescript(self, sql):
        if self._backend == "lite":
            self._raw.executescript(sql)
        else:
            for stmt in sql.split(";"):
                stmt = stmt.strip()
                if stmt:
                    self.execute(stmt)

    @property
    def backend(self):
        return self._backend


def get_conn():
    if not hasattr(_local, "conn") or _local.conn is None:
        _local.conn = _Conn()
    return _local.conn


def _lastrowid(cur):
    if _is_pg():
        return cur.fetchone()["id"]
    return cur.lastrowid


def init_db():
    conn = get_conn()
    if _is_pg():
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS accounts (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                yandex_token TEXT NOT NULL,
                duid TEXT DEFAULT '',
                edadeal_uid TEXT DEFAULT '',
                login TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                last_spin TEXT,
                last_balance INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS prizes (
                id SERIAL PRIMARY KEY,
                account_id INTEGER NOT NULL,
                prize_title TEXT DEFAULT '',
                prize_img TEXT DEFAULT '',
                balance_after INTEGER DEFAULT 0,
                spun_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tracking_accounts (
                id SERIAL PRIMARY KEY,
                yandex_token TEXT NOT NULL,
                login TEXT DEFAULT '',
                duid TEXT DEFAULT '',
                edadeal_uid TEXT DEFAULT '',
                last_balance INTEGER,
                last_checked TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tracking_log (
                id SERIAL PRIMARY KEY,
                event_time TEXT,
                message TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS promo_codes (
                id SERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                account_id INTEGER NOT NULL,
                campaign_id TEXT NOT NULL,
                code TEXT NOT NULL,
                created_at TEXT
            )
        """)
    else:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                yandex_token TEXT NOT NULL,
                duid TEXT DEFAULT '',
                edadeal_uid TEXT DEFAULT '',
                login TEXT DEFAULT '',
                status TEXT DEFAULT 'active',
                last_spin TEXT,
                last_balance INTEGER DEFAULT 0,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS prizes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                prize_title TEXT DEFAULT '',
                prize_img TEXT DEFAULT '',
                balance_after INTEGER DEFAULT 0,
                spun_at TEXT
            );
            CREATE TABLE IF NOT EXISTS tracking_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                yandex_token TEXT NOT NULL,
                login TEXT DEFAULT '',
                duid TEXT DEFAULT '',
                edadeal_uid TEXT DEFAULT '',
                last_balance INTEGER,
                last_checked TEXT,
                status TEXT DEFAULT 'active',
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS tracking_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_time TEXT,
                message TEXT
            );
            CREATE TABLE IF NOT EXISTS promo_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                campaign_id TEXT NOT NULL,
                code TEXT NOT NULL,
                created_at TEXT
            );
        """)
    conn.commit()


def add_user(telegram_id, username="", first_name=""):
    conn = get_conn()
    now = datetime.now().isoformat()
    if _is_pg():
        conn.execute(
            "INSERT INTO users (telegram_id, username, first_name, created_at) VALUES (?,?,?,?) ON CONFLICT DO NOTHING",
            (telegram_id, username, first_name, now),
        )
    else:
        conn.execute(
            "INSERT OR IGNORE INTO users (telegram_id, username, first_name, created_at) VALUES (?,?,?,?)",
            (telegram_id, username, first_name, now),
        )
    conn.commit()


def add_account(user_id, yandex_token, login="", duid="", edadeal_uid=""):
    conn = get_conn()
    now = datetime.now().isoformat()
    if _is_pg():
        cur = conn.execute(
            "INSERT INTO accounts (user_id, yandex_token, login, duid, edadeal_uid, status, created_at) VALUES (?,?,?,?,?,?,?) RETURNING id",
            (user_id, yandex_token, login, duid, edadeal_uid, "active", now),
        )
    else:
        cur = conn.execute(
            "INSERT INTO accounts (user_id, yandex_token, login, duid, edadeal_uid, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (user_id, yandex_token, login, duid, edadeal_uid, "active", now),
        )
    conn.commit()
    return _lastrowid(cur)


def remove_account(account_id, user_id):
    conn = get_conn()
    conn.execute("DELETE FROM prizes WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE id=? AND user_id=?", (account_id, user_id))
    conn.commit()


def get_user_accounts(user_id):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM accounts WHERE user_id=? ORDER BY created_at DESC", (user_id,)
    ).fetchall()


def get_account(account_id):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM accounts WHERE id=?", (account_id,)
    ).fetchone()


def get_account_by_login(user_id, login):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM accounts WHERE user_id=? AND login=?",
        (user_id, login),
    ).fetchone()


def get_all_active_accounts():
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM accounts WHERE status='active' ORDER BY id"
    ).fetchall()


def update_account_status(account_id, status):
    conn = get_conn()
    conn.execute("UPDATE accounts SET status=? WHERE id=?", (status, account_id))
    conn.commit()


def update_account_spin(account_id, balance):
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE accounts SET last_spin=?, last_balance=? WHERE id=?",
        (now, balance, account_id),
    )
    conn.commit()


def update_account_balance(account_id, balance):
    conn = get_conn()
    conn.execute("UPDATE accounts SET last_balance=? WHERE id=?", (balance, account_id))
    conn.commit()


def add_prize(account_id, prize_title, prize_img, balance_after):
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        "INSERT INTO prizes (account_id, prize_title, prize_img, balance_after, spun_at) VALUES (?,?,?,?,?)",
        (account_id, prize_title, prize_img, balance_after, now),
    )
    conn.commit()


def get_account_prizes(account_id, limit=10):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM prizes WHERE account_id=? ORDER BY spun_at DESC LIMIT ?",
        (account_id, limit),
    ).fetchall()


def get_all_users():
    conn = get_conn()
    return conn.execute(
        "SELECT telegram_id, username, first_name FROM users ORDER BY telegram_id"
    ).fetchall()


def get_user_stats(user_id):
    conn = get_conn()
    total = conn.execute(
        "SELECT COUNT(*) AS v FROM accounts WHERE user_id=?", (user_id,)
    ).fetchone()["v"]
    active = conn.execute(
        "SELECT COUNT(*) AS v FROM accounts WHERE user_id=? AND status='active'", (user_id,)
    ).fetchone()["v"]
    spins = conn.execute(
        "SELECT COUNT(*) AS v FROM prizes p JOIN accounts a ON p.account_id = a.id WHERE a.user_id=?",
        (user_id,),
    ).fetchone()["v"]
    balance = conn.execute(
        "SELECT COALESCE(SUM(last_balance),0) AS v FROM accounts WHERE user_id=? AND status='active'",
        (user_id,),
    ).fetchone()["v"]
    return {
        "total_accounts": total,
        "active_accounts": active,
        "total_spins": spins,
        "total_balance": balance,
    }


def set_tracking_account(yandex_token, login="", duid="", edadeal_uid=""):
    """Replace the single tracking account (only one at a time)."""
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute("DELETE FROM tracking_accounts")
    conn.execute("DELETE FROM tracking_log")
    if _is_pg():
        cur = conn.execute(
            "INSERT INTO tracking_accounts (yandex_token, login, duid, edadeal_uid, last_balance, created_at) VALUES (?,?,?,?,?,?) RETURNING id",
            (yandex_token, login, duid, edadeal_uid, None, now),
        )
    else:
        cur = conn.execute(
            "INSERT INTO tracking_accounts (yandex_token, login, duid, edadeal_uid, last_balance, created_at) VALUES (?,?,?,?,?,?)",
            (yandex_token, login, duid, edadeal_uid, None, now),
        )
    conn.commit()
    return _lastrowid(cur)


def get_tracking_account():
    conn = get_conn()
    return conn.execute("SELECT * FROM tracking_accounts LIMIT 1").fetchone()


def delete_tracking_account():
    conn = get_conn()
    conn.execute("DELETE FROM tracking_accounts")
    conn.execute("DELETE FROM tracking_log")
    conn.commit()


def update_tracking_balance(account_id, balance):
    conn = get_conn()
    now = datetime.now().isoformat()
    conn.execute(
        "UPDATE tracking_accounts SET last_balance=?, last_checked=? WHERE id=?",
        (balance, now, account_id),
    )
    conn.commit()


def update_tracking_status(account_id, status):
    conn = get_conn()
    conn.execute(
        "UPDATE tracking_accounts SET status=? WHERE id=?", (status, account_id)
    )
    conn.commit()


def add_tracking_log(message):
    conn = get_conn()
    now = datetime.now().isoformat()
    if _is_pg():
        conn.execute(
            "INSERT INTO tracking_log (event_time, message) VALUES (?,?) RETURNING id",
            (now, message),
        )
    else:
        conn.execute(
            "INSERT INTO tracking_log (event_time, message) VALUES (?,?)",
            (now, message),
        )
    conn.commit()


def get_tracking_log(limit=50):
    conn = get_conn()
    return conn.execute(
        "SELECT event_time, message FROM tracking_log ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def save_promo_code(user_id, account_id, campaign_id, code):
    conn = get_conn()
    now = datetime.now().isoformat()
    if _is_pg():
        conn.execute(
            "INSERT INTO promo_codes (user_id, account_id, campaign_id, code, created_at) VALUES (?,?,?,?,?)",
            (user_id, account_id, campaign_id, code, now),
        )
    else:
        conn.execute(
            "INSERT INTO promo_codes (user_id, account_id, campaign_id, code, created_at) VALUES (?,?,?,?,?)",
            (user_id, account_id, campaign_id, code, now),
        )
    conn.commit()


def get_user_promo_codes(user_id):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM promo_codes WHERE user_id=? ORDER BY id DESC",
        (user_id,),
    ).fetchall()


def get_promo_code(account_id, campaign_id):
    conn = get_conn()
    return conn.execute(
        "SELECT * FROM promo_codes WHERE account_id=? AND campaign_id=?",
        (account_id, campaign_id),
    ).fetchone()


def delete_promo_code(account_id, campaign_id):
    conn = get_conn()
    conn.execute(
        "DELETE FROM promo_codes WHERE account_id=? AND campaign_id=?",
        (account_id, campaign_id),
    )
    conn.commit()


def clear_user_promo_codes(user_id):
    conn = get_conn()
    conn.execute("DELETE FROM promo_codes WHERE user_id=?", (user_id,))
    conn.commit()

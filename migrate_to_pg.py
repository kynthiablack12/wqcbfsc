"""
Export all data from SQLite → PostgreSQL.

Usage:
    DATABASE_URL=postgresql://user:pass@host/db python migrate_to_pg.py

Connects to local SQLite, creates tables in PostgreSQL,
and copies all users, accounts, prizes preserving IDs.
"""
import os
import sys

import sqlite3

# Force PostgreSQL mode
os.environ["DATABASE_URL"] = os.environ.get("DATABASE_URL", "")
if not os.environ["DATABASE_URL"]:
    print("❌ Set DATABASE_URL=postgresql://... first")
    sys.exit(1)

import psycopg2
import psycopg2.extras

import database as db

SQLITE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "edadil.db")


def read_sqlite():
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row

    users = conn.execute("SELECT * FROM users").fetchall()
    accounts = conn.execute("SELECT * FROM accounts").fetchall()
    prizes = conn.execute("SELECT * FROM prizes").fetchall()

    conn.close()
    return users, accounts, prizes


def write_pg(users, accounts, prizes):
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    conn.autocommit = False
    cur = conn.cursor()

    # Init tables via database module (pg DDL)
    db.init_db()

    # Users
    cur.execute("TRUNCATE TABLE users CASCADE")
    for u in users:
        cur.execute(
            "INSERT INTO users (telegram_id, username, first_name, created_at) VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (u["telegram_id"], u["username"], u["first_name"], u["created_at"]),
        )
    print(f"  users: {len(users)}")

    # Accounts (with explicit id)
    cur.execute("TRUNCATE TABLE accounts CASCADE")
    for a in accounts:
        cur.execute(
            "INSERT INTO accounts (id, user_id, yandex_token, duid, edadeal_uid, login, status, last_spin, last_balance, created_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (a["id"], a["user_id"], a["yandex_token"], a["duid"], a["edadeal_uid"],
             a["login"], a["status"], a["last_spin"], a["last_balance"], a["created_at"]),
        )
    # Reset the sequence to max(id)+1
    cur.execute("SELECT setval('accounts_id_seq', COALESCE((SELECT MAX(id) FROM accounts), 1))")
    print(f"  accounts: {len(accounts)}")

    # Prizes
    cur.execute("TRUNCATE TABLE prizes CASCADE")
    for p in prizes:
        cur.execute(
            "INSERT INTO prizes (id, account_id, prize_title, prize_img, balance_after, spun_at) VALUES (%s,%s,%s,%s,%s,%s)",
            (p["id"], p["account_id"], p["prize_title"], p["prize_img"], p["balance_after"], p["spun_at"]),
        )
    cur.execute("SELECT setval('prizes_id_seq', COALESCE((SELECT MAX(id) FROM prizes), 1))")
    print(f"  prizes: {len(prizes)}")

    conn.commit()
    conn.close()


def verify():
    """Quick check — compare counts."""
    lite = sqlite3.connect(SQLITE_PATH)
    pg = psycopg2.connect(os.environ["DATABASE_URL"])
    pg_cur = pg.cursor()

    for table in ["users", "accounts", "prizes"]:
        lc = lite.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        pg_cur.execute(f"SELECT COUNT(*) FROM {table}")
        pc = pg_cur.fetchone()[0]
        ok = "✅" if lc == pc else "❌"
        print(f"  {ok} {table}: SQLite={lc}  PG={pc}")

    lite.close()
    pg.close()


if __name__ == "__main__":
    print("📤 Reading SQLite...")
    users, accounts, prizes = read_sqlite()
    print(f"  users={len(users)}  accounts={len(accounts)}  prizes={len(prizes)}")

    print("📥 Writing to PostgreSQL...")
    write_pg(users, accounts, prizes)

    print("🔍 Verifying...")
    verify()

    print("✅ Migration complete!")

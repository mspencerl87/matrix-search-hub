import sqlite3
import time

# Deliberately minimal and unencrypted: just enough to know a user has used
# this app before and which Matrix device belongs to them, so the UI can
# show "unlock your vault" instead of "set up a new one". No OAuth tokens or
# message data live here - those are inside each user's encrypted vault
# (see vault.py), which is the whole point of this split.
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS admins (
    user_id TEXT PRIMARY KEY,
    added_by TEXT,
    created_at REAL NOT NULL
);
"""


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_user(conn, user_id: str, device_id: str):
    conn.execute(
        """
        INSERT INTO users (user_id, device_id, created_at) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET device_id=excluded.device_id
        """,
        (user_id, device_id, time.time()),
    )
    conn.commit()


def get_user(conn, user_id: str):
    cur = conn.execute("SELECT user_id, device_id FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return {"user_id": row[0], "device_id": row[1]} if row else None


def all_users(conn):
    cur = conn.execute("SELECT user_id, device_id, created_at FROM users ORDER BY created_at DESC")
    return [{"user_id": r[0], "device_id": r[1], "created_at": r[2]} for r in cur.fetchall()]


def delete_user(conn, user_id: str):
    conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
    conn.commit()


def add_admin(conn, user_id: str, added_by: str):
    conn.execute(
        "INSERT INTO admins (user_id, added_by, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO NOTHING",
        (user_id, added_by, time.time()),
    )
    conn.commit()


def remove_admin(conn, user_id: str):
    conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    conn.commit()


def is_dynamic_admin(conn, user_id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    return cur.fetchone() is not None


def list_admins(conn):
    cur = conn.execute("SELECT user_id, added_by, created_at FROM admins ORDER BY created_at")
    return [{"user_id": r[0], "added_by": r[1], "created_at": r[2]} for r in cur.fetchall()]

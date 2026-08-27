import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    device_id TEXT NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at REAL,
    created_at REAL NOT NULL
);
"""


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def upsert_user(conn, user_id, device_id, access_token, refresh_token, expires_at):
    conn.execute(
        """
        INSERT INTO users (user_id, device_id, access_token, refresh_token, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            device_id=excluded.device_id,
            access_token=excluded.access_token,
            refresh_token=excluded.refresh_token,
            expires_at=excluded.expires_at
        """,
        (user_id, device_id, access_token, refresh_token, expires_at, time.time()),
    )
    conn.commit()


def update_tokens(conn, user_id, access_token, refresh_token, expires_at):
    conn.execute(
        "UPDATE users SET access_token=?, refresh_token=?, expires_at=? WHERE user_id=?",
        (access_token, refresh_token, expires_at, user_id),
    )
    conn.commit()


_COLUMNS = ["user_id", "device_id", "access_token", "refresh_token", "expires_at"]


def get_user(conn, user_id):
    cur = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM users WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    return dict(zip(_COLUMNS, row)) if row else None


def all_users(conn):
    cur = conn.execute(f"SELECT {', '.join(_COLUMNS)} FROM users")
    return [dict(zip(_COLUMNS, row)) for row in cur.fetchall()]

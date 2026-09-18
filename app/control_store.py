import datetime
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

-- One row per (day, metric), incremented in place - stays a few hundred
-- rows forever regardless of how many searches actually happen, so
-- "all-time" totals (a SUM over this table) stay cheap indefinitely.
CREATE TABLE IF NOT EXISTS daily_metrics (
    date TEXT NOT NULL,
    metric TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (date, metric)
);
"""


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _migrate(conn):
    # Cached message/room counts, written opportunistically whenever a
    # user's own /api/status is polled - lets org-wide totals be computed
    # without ever touching a vault, even for users who are currently
    # locked. Added via migration since `users` may already exist from
    # before this column was introduced.
    cols = [row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "last_known_messages" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN last_known_messages INTEGER")
    if "last_known_rooms" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN last_known_rooms INTEGER")


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


def _today() -> str:
    return datetime.date.today().isoformat()


def increment_metric(conn, metric: str, amount: int = 1):
    today = _today()
    conn.execute(
        """
        INSERT INTO daily_metrics (date, metric, value) VALUES (?, ?, ?)
        ON CONFLICT(date, metric) DO UPDATE SET value = value + excluded.value
        """,
        (today, metric, amount),
    )
    conn.commit()


def metric_today(conn, metric: str) -> int:
    row = conn.execute("SELECT value FROM daily_metrics WHERE date = ? AND metric = ?", (_today(), metric)).fetchone()
    return row[0] if row else 0


def metric_all_time(conn, metric: str) -> int:
    row = conn.execute("SELECT SUM(value) FROM daily_metrics WHERE metric = ?", (metric,)).fetchone()
    return row[0] or 0


def update_user_stats_cache(conn, user_id: str, messages: int, rooms: int):
    conn.execute(
        "UPDATE users SET last_known_messages = ?, last_known_rooms = ? WHERE user_id = ?",
        (messages, rooms, user_id),
    )
    conn.commit()


def total_indexed_stats(conn):
    row = conn.execute(
        "SELECT COALESCE(SUM(last_known_messages), 0), COALESCE(SUM(last_known_rooms), 0) FROM users"
    ).fetchone()
    return {"messages": row[0], "rooms": row[1]}


def total_user_count(conn) -> int:
    return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

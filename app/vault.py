import os

from sqlcipher3 import dbapi2 as sqlcipher

from app import config
from app.paths import user_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    event_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    room_name TEXT,
    sender TEXT,
    body TEXT,
    origin_server_ts INTEGER
);

CREATE INDEX IF NOT EXISTS idx_messages_room_ts ON messages(room_id, origin_server_ts);
CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(origin_server_ts);

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    body, sender, room_name,
    content='messages', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, body, sender, room_name)
    VALUES (new.rowid, new.body, new.sender, new.room_name);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body, sender, room_name)
    VALUES ('delete', old.rowid, old.body, old.sender, old.room_name);
END;

CREATE TABLE IF NOT EXISTS oauth (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    device_id TEXT NOT NULL,
    access_token TEXT NOT NULL,
    refresh_token TEXT,
    expires_at REAL
);
"""


class VaultError(Exception):
    pass


class WrongPassphrase(VaultError):
    pass


def path_for(user_id: str) -> str:
    return os.path.join(user_dir(user_id), "vault.db")


def exists(user_id: str) -> bool:
    return os.path.exists(path_for(user_id))


def open_vault(user_id: str, passphrase: str):
    """Open (or create) a user's encrypted vault. Raises WrongPassphrase if
    the file already exists and the passphrase doesn't decrypt it."""
    p = path_for(user_id)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    is_new = not os.path.exists(p)

    conn = sqlcipher.connect(p, check_same_thread=False)
    # PRAGMA doesn't support bound parameters - inline it, escaping quotes
    # the same way a SQL string literal would (doubling embedded ' chars).
    escaped = passphrase.replace("'", "''")
    conn.execute(f"PRAGMA key = '{escaped}'")
    try:
        conn.execute("SELECT count(*) FROM sqlite_master")
    except sqlcipher.DatabaseError as e:
        conn.close()
        raise WrongPassphrase("Incorrect passphrase") from e

    conn.executescript(SCHEMA)
    conn.commit()
    if is_new:
        pass  # nothing further to seed - oauth row gets inserted by set_oauth()
    return conn


def verify_passphrase(user_id: str, passphrase: str) -> bool:
    """Checks a passphrase against an existing vault without disturbing any
    already-open connection to it (used to confirm a passphrase-change
    request before rekeying the live connection)."""
    try:
        conn = open_vault(user_id, passphrase)
    except WrongPassphrase:
        return False
    conn.close()
    return True


def change_passphrase(conn, new_passphrase: str) -> None:
    """Rekeys an already-open vault connection in place. Caller must verify
    the current passphrase first - this itself doesn't check anything."""
    escaped = new_passphrase.replace("'", "''")
    conn.execute(f"PRAGMA rekey = '{escaped}'")


def get_oauth(conn):
    cur = conn.execute("SELECT device_id, access_token, refresh_token, expires_at FROM oauth WHERE id = 1")
    row = cur.fetchone()
    if not row:
        return None
    return {"device_id": row[0], "access_token": row[1], "refresh_token": row[2], "expires_at": row[3]}


def set_oauth(conn, device_id: str, access_token: str, refresh_token: str | None, expires_at: float):
    conn.execute(
        """
        INSERT INTO oauth (id, device_id, access_token, refresh_token, expires_at)
        VALUES (1, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            device_id=excluded.device_id,
            access_token=excluded.access_token,
            refresh_token=excluded.refresh_token,
            expires_at=excluded.expires_at
        """,
        (device_id, access_token, refresh_token, expires_at),
    )
    conn.commit()

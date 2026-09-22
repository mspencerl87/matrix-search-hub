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

-- Room-level metadata, kept separate from messages since is_direct is a
-- property of the room, not of any individual message. Whether a room is
-- a DM is decided by m.direct account data, not by membership count, so
-- it's fetched via list_direct_rooms() and cached here rather than
-- guessed at from room state. avatar_mxc is an mxc:// content URI (nio's
-- gen_avatar_url - the room's own avatar if set, otherwise the other
-- member's for a DM), resolved to an actual image via /api/avatar.
-- read_marker_ts is this account's own most recent read-receipt timestamp
-- in that room (m.read or the private m.read.private variant - either
-- means the user genuinely read up to that point, from any of their
-- devices) - used to compute unread counts ourselves instead of trusting
-- nio's per-device unread_notifications, which only reflects this app's
-- own bot device (which never reads anything) and is therefore useless
-- for this purpose.
-- is_space marks a room whose m.room.create event declared room_type
-- "m.space" - Matrix's actual container/folder concept (Element's
-- sidebar groupings). It's permanent once set: a room's type can't
-- change after creation, so this only ever gets set to 1, never back to 0.
CREATE TABLE IF NOT EXISTS rooms (
    room_id TEXT PRIMARY KEY,
    room_name TEXT,
    is_direct INTEGER NOT NULL DEFAULT 0,
    avatar_mxc TEXT,
    read_marker_ts INTEGER,
    is_space INTEGER NOT NULL DEFAULT 0
);

-- Space -> child room edges, from that space's own m.space.child state
-- events (the same mechanism Element's sidebar hierarchy is built from).
-- Rebuilt from scratch on every full resync (see resync_history()) rather
-- than patched incrementally, since a live sync only announces removals
-- as an event, not an absence, and it's simpler to treat each full sync
-- as the authoritative current snapshot.
CREATE TABLE IF NOT EXISTS space_children (
    space_id TEXT NOT NULL,
    child_room_id TEXT NOT NULL,
    PRIMARY KEY (space_id, child_room_id)
);
"""


def _migrate(conn):
    """Adds columns to tables that already existed before that column was
    introduced - CREATE TABLE IF NOT EXISTS above is a no-op against an
    existing table, so this covers vaults created by an older version."""
    cols = {row[1] for row in conn.execute("PRAGMA table_info(rooms)").fetchall()}
    if "avatar_mxc" not in cols:
        conn.execute("ALTER TABLE rooms ADD COLUMN avatar_mxc TEXT")
        conn.commit()
    if "read_marker_ts" not in cols:
        conn.execute("ALTER TABLE rooms ADD COLUMN read_marker_ts INTEGER")
        conn.commit()
    if "is_space" not in cols:
        conn.execute("ALTER TABLE rooms ADD COLUMN is_space INTEGER NOT NULL DEFAULT 0")
        conn.commit()


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
    _migrate(conn)
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

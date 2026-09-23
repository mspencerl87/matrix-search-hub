import os
import sqlite3

import pytest


os.environ.setdefault("MATRIX_HOMESERVER", "https://matrix.example.test")
os.environ.setdefault("BASE_URL", "https://search.example.test")
os.environ.setdefault("SESSION_SECRET", "test-only-session-secret")


SEARCH_SCHEMA = """
CREATE TABLE messages (
    event_id TEXT PRIMARY KEY,
    room_id TEXT NOT NULL,
    room_name TEXT,
    sender TEXT,
    body TEXT,
    origin_server_ts INTEGER
);
CREATE VIRTUAL TABLE messages_fts USING fts5(
    body, sender, room_name,
    content='messages', content_rowid='rowid'
);
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, body, sender, room_name)
    VALUES (new.rowid, new.body, new.sender, new.room_name);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, body, sender, room_name)
    VALUES ('delete', old.rowid, old.body, old.sender, old.room_name);
END;
CREATE TABLE rooms (
    room_id TEXT PRIMARY KEY,
    room_name TEXT,
    is_direct INTEGER NOT NULL DEFAULT 0,
    avatar_mxc TEXT,
    read_marker_ts INTEGER
);
"""


@pytest.fixture
def search_conn():
    conn = sqlite3.connect(":memory:")
    conn.executescript(SEARCH_SCHEMA)
    yield conn
    conn.close()

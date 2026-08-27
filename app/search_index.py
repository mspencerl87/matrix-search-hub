import sqlite3

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

CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    body, sender, room_name,
    content='messages', content_rowid='rowid'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, body, sender, room_name)
    VALUES (new.rowid, new.body, new.sender, new.room_name);
END;
"""


def init_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def add_message(conn, event_id, room_id, room_name, sender, body, ts, commit=True):
    if not body:
        return
    conn.execute(
        "INSERT OR IGNORE INTO messages (event_id, room_id, room_name, sender, body, origin_server_ts) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (event_id, room_id, room_name, sender, body, ts),
    )
    if commit:
        conn.commit()


def _fts_query(raw: str) -> str:
    tokens = raw.split()
    escaped = ['"{}"'.format(t.replace('"', '""')) for t in tokens if t]
    return " ".join(escaped) if escaped else '""'


def search(conn, query: str, limit: int = 50, room_id: str = None):
    fts_q = _fts_query(query)
    sql = """
        SELECT m.event_id, m.room_id, m.room_name, m.sender, m.body, m.origin_server_ts,
               snippet(messages_fts, 0, '[[', ']]', '...', 12) AS snippet
        FROM messages_fts
        JOIN messages m ON m.rowid = messages_fts.rowid
        WHERE messages_fts MATCH ?
    """
    params = [fts_q]
    if room_id:
        sql += " AND m.room_id = ?"
        params.append(room_id)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    rooms = conn.execute("SELECT COUNT(DISTINCT room_id) FROM messages").fetchone()[0]
    return {"indexed_messages": total, "rooms": rooms}

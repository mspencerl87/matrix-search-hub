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


SORT_ORDERS = {
    "relevance": "rank",
    "newest": "m.origin_server_ts DESC",
    "oldest": "m.origin_server_ts ASC",
}


def search(conn, query: str, limit: int = 50, room_id: str = None, since_ts: int = None, sort: str = "relevance"):
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
    if since_ts is not None:
        sql += " AND m.origin_server_ts >= ?"
        params.append(since_ts)
    order_by = SORT_ORDERS.get(sort, "rank")
    sql += f" ORDER BY {order_by} LIMIT ?"
    params.append(limit)
    cur = conn.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def get_stats(conn):
    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    rooms = conn.execute("SELECT COUNT(DISTINCT room_id) FROM messages").fetchone()[0]
    return {"indexed_messages": total, "rooms": rooms}


def list_rooms(conn):
    cur = conn.execute(
        "SELECT room_id, MAX(room_name) AS room_name FROM messages GROUP BY room_id ORDER BY room_name COLLATE NOCASE"
    )
    return [{"room_id": r[0], "room_name": r[1] or r[0]} for r in cur.fetchall()]


def clear_all(conn):
    """Wipes the message index (and, via the delete trigger, its FTS index)
    without touching the oauth table - a resync afterward rebuilds it."""
    cur = conn.execute("DELETE FROM messages")
    conn.commit()
    return cur.rowcount


def prune_older_than(conn, cutoff_ts_ms: int):
    cur = conn.execute("DELETE FROM messages WHERE origin_server_ts < ?", (cutoff_ts_ms,))
    conn.commit()
    return cur.rowcount


def upsert_room(conn, room_id: str, room_name: str, is_direct: bool, commit: bool = True):
    conn.execute(
        """
        INSERT INTO rooms (room_id, room_name, is_direct) VALUES (?, ?, ?)
        ON CONFLICT(room_id) DO UPDATE SET room_name = excluded.room_name, is_direct = excluded.is_direct
        """,
        (room_id, room_name, int(is_direct)),
    )
    if commit:
        conn.commit()


def recent_conversations(conn, limit: int = 10):
    """Most recently active room per category, each with a preview of its
    last message. is_direct comes from the rooms table (set via
    list_direct_rooms()); a room we haven't classified yet defaults to
    "not a DM" rather than risking miscategorizing a real conversation."""
    cur = conn.execute(
        """
        WITH ranked AS (
            SELECT m.room_id, m.room_name, m.sender, m.body, m.origin_server_ts,
                   COALESCE(r.is_direct, 0) AS is_direct,
                   ROW_NUMBER() OVER (PARTITION BY m.room_id ORDER BY m.origin_server_ts DESC) AS rn
            FROM messages m
            LEFT JOIN rooms r ON r.room_id = m.room_id
        )
        SELECT room_id, room_name, sender, body, origin_server_ts, is_direct
        FROM ranked
        WHERE rn = 1
        ORDER BY origin_server_ts DESC
        """
    )
    direct, rooms = [], []
    for room_id, room_name, sender, body, ts, is_direct in cur.fetchall():
        item = {
            "room_id": room_id,
            "room_name": room_name or room_id,
            "sender": sender,
            "body": body,
            "origin_server_ts": ts,
        }
        bucket = direct if is_direct else rooms
        if len(bucket) < limit:
            bucket.append(item)
        if len(direct) >= limit and len(rooms) >= limit:
            break
    return {"direct": direct, "rooms": rooms}

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

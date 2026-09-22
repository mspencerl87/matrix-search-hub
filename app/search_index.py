import re


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


def upsert_room(conn, room_id: str, room_name: str, is_direct: bool, avatar_mxc: str | None = None, commit: bool = True):
    conn.execute(
        """
        INSERT INTO rooms (room_id, room_name, is_direct, avatar_mxc) VALUES (?, ?, ?, ?)
        ON CONFLICT(room_id) DO UPDATE SET
            room_name = excluded.room_name,
            is_direct = excluded.is_direct,
            avatar_mxc = excluded.avatar_mxc
        """,
        (room_id, room_name, int(is_direct), avatar_mxc),
    )
    if commit:
        conn.commit()


def update_read_marker(conn, room_id: str, ts: int, commit: bool = True):
    """Records this account's own read-receipt timestamp for a room, taking
    the max against whatever's already stored so a late/out-of-order
    receipt can never move the marker backwards."""
    conn.execute(
        """
        INSERT INTO rooms (room_id, read_marker_ts) VALUES (?, ?)
        ON CONFLICT(room_id) DO UPDATE SET
            read_marker_ts = MAX(COALESCE(read_marker_ts, 0), excluded.read_marker_ts)
        """,
        (room_id, ts),
    )
    if commit:
        conn.commit()


def unread_counts_by_room(conn, self_user_id: str):
    """Per-room unread/mention counts, computed from this account's own
    read-receipt position (read_marker_ts) rather than nio's built-in
    unread_notifications/unread_highlights - those reflect this app's own
    bot device, which never reads anything and so is always "maximally
    unread", not what the user has actually read elsewhere (e.g. in
    Element, which by default sends *private* read receipts that are
    per-device and never seen by this app's device at all).

    A room with no read marker recorded yet (no receipt observed since
    this feature shipped, or since the room was first joined) is left out
    entirely rather than reported as fully unread with no real baseline.

    "Mentions" here is a plain-text heuristic (the user's own localpart or
    a literal "@room" appearing as a whole word in the message) since
    evaluating the account's real push rules isn't something this app has
    access to - close to, but not exactly, what Element itself highlights.
    Matching is done in Python with a word-boundary regex rather than SQL
    LIKE, since a plain substring match on a short localpart (e.g. "me")
    also fires on unrelated words that merely contain it (e.g. "mention")."""
    localpart = self_user_id.split(":", 1)[0].lstrip("@")
    mention_re = re.compile(r"\b(" + re.escape(localpart) + r"|@room)\b", re.IGNORECASE)

    cur = conn.execute(
        """
        SELECT m.room_id, m.body
        FROM messages m
        JOIN rooms r ON r.room_id = m.room_id
        WHERE r.read_marker_ts IS NOT NULL
          AND m.origin_server_ts > r.read_marker_ts
          AND m.sender != ?
        """,
        (self_user_id,),
    )
    counts: dict[str, dict[str, int]] = {}
    for room_id, body in cur.fetchall():
        entry = counts.setdefault(room_id, {"unread_count": 0, "mention_count": 0})
        entry["unread_count"] += 1
        if body and mention_re.search(body):
            entry["mention_count"] += 1
    return counts


def _last_message_rows(conn):
    """One row per room_id: its most recent message plus room metadata.
    is_direct/avatar_mxc come from the rooms table (set via
    list_direct_rooms()/gen_avatar_url); a room we haven't classified yet
    defaults to "not a DM" rather than risking miscategorizing a real
    conversation."""
    cur = conn.execute(
        """
        WITH ranked AS (
            SELECT m.room_id, m.room_name, m.sender, m.body, m.origin_server_ts,
                   COALESCE(r.is_direct, 0) AS is_direct, r.avatar_mxc,
                   ROW_NUMBER() OVER (PARTITION BY m.room_id ORDER BY m.origin_server_ts DESC) AS rn
            FROM messages m
            LEFT JOIN rooms r ON r.room_id = m.room_id
        )
        SELECT room_id, room_name, sender, body, origin_server_ts, is_direct, avatar_mxc
        FROM ranked
        WHERE rn = 1
        ORDER BY origin_server_ts DESC
        """
    )
    return cur.fetchall()


def recent_conversations(conn, limit: int = 10):
    """Most recently active room per category, each with a preview of its
    last message."""
    direct, rooms = [], []
    for room_id, room_name, sender, body, ts, is_direct, avatar_mxc in _last_message_rows(conn):
        item = {
            "room_id": room_id,
            "room_name": room_name or room_id,
            "sender": sender,
            "body": body,
            "origin_server_ts": ts,
            "avatar_mxc": avatar_mxc,
        }
        bucket = direct if is_direct else rooms
        if len(bucket) < limit:
            bucket.append(item)
        if len(direct) >= limit and len(rooms) >= limit:
            break
    return {"direct": direct, "rooms": rooms}


def last_message_by_room(conn):
    """Every room's latest message, keyed by room_id - for building previews
    of a room subset chosen by some other criterion (e.g. unread status)
    rather than the "most recent N" that recent_conversations() bucketizes."""
    result = {}
    for room_id, room_name, sender, body, ts, is_direct, avatar_mxc in _last_message_rows(conn):
        result[room_id] = {
            "room_name": room_name or room_id,
            "sender": sender,
            "body": body,
            "origin_server_ts": ts,
            "is_direct": bool(is_direct),
            "avatar_mxc": avatar_mxc,
        }
    return result

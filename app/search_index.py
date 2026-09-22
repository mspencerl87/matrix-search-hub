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


def mark_space(conn, room_id: str, commit: bool = True):
    """Records that a room is a Space (room_type "m.space" on its own
    m.room.create event) - permanent once set, so this never gets cleared."""
    conn.execute(
        "INSERT INTO rooms (room_id, is_space) VALUES (?, 1) "
        "ON CONFLICT(room_id) DO UPDATE SET is_space = 1",
        (room_id,),
    )
    if commit:
        conn.commit()


def clear_space_children(conn, commit: bool = True):
    """Wipes the space hierarchy edges before a full resync repopulates
    them - see the space_children schema comment for why this is a
    delete-then-rebuild rather than an incremental patch."""
    conn.execute("DELETE FROM space_children")
    if commit:
        conn.commit()


def add_space_child(conn, space_id: str, child_room_id: str, commit: bool = True):
    conn.execute(
        "INSERT OR IGNORE INTO space_children (space_id, child_room_id) VALUES (?, ?)",
        (space_id, child_room_id),
    )
    if commit:
        conn.commit()


def room_tree(conn):
    """Every non-DM room this account knows about, plus the space->child
    edges needed to render them as a hierarchy. Direct messages are left
    out entirely - Matrix's space model doesn't organize DMs at all, so
    they're covered by the recent-conversations/unread views instead."""
    rooms_cur = conn.execute(
        "SELECT room_id, room_name, avatar_mxc, is_space FROM rooms WHERE COALESCE(is_direct, 0) = 0"
    )
    rooms = {
        row[0]: {"room_id": row[0], "room_name": row[1] or row[0], "avatar_mxc": row[2], "is_space": bool(row[3])}
        for row in rooms_cur.fetchall()
    }
    edges = conn.execute("SELECT space_id, child_room_id FROM space_children").fetchall()
    return rooms, [(row[0], row[1]) for row in edges]


def build_room_tree(rooms: dict, edges: list, counts: dict):
    """Pure tree assembly from room_tree()'s output plus
    unread_counts_by_room(): a list of root nodes (rooms with no known
    parent), each a dict with room_id/room_name/is_space/avatar_mxc/
    unread_count/mention_count/children, the last three rolled up through
    descendants for a space so a collapsed header can show whether
    anything inside it needs attention.

    Guards against a cyclical space graph (a space that's transitively its
    own descendant - shouldn't occur on real data, but would otherwise
    recurse forever) by refusing to re-enter an ancestor already on the
    current path; such a space just renders childless the second time."""
    children_by_parent: dict[str, list[str]] = {}
    child_ids: set[str] = set()
    for space_id, child_room_id in edges:
        if space_id not in rooms or child_room_id not in rooms:
            continue  # the other end is a room we've never classified - skip rather than dangle
        children_by_parent.setdefault(space_id, []).append(child_room_id)
        child_ids.add(child_room_id)

    def sort_key(room_id: str):
        info = rooms[room_id]
        return (not info["is_space"], info["room_name"].lower())

    def build_node(room_id: str, ancestors: frozenset) -> dict:
        info = rooms[room_id]
        room_counts = counts.get(room_id, {"unread_count": 0, "mention_count": 0})
        node = {
            "room_id": room_id,
            "room_name": info["room_name"],
            "is_space": info["is_space"],
            "avatar_mxc": info["avatar_mxc"],
            "unread_count": room_counts["unread_count"],
            "mention_count": room_counts["mention_count"],
            "children": [],
        }
        if info["is_space"] and room_id not in ancestors:
            child_ids_sorted = sorted(children_by_parent.get(room_id, []), key=sort_key)
            node["children"] = [build_node(cid, ancestors | {room_id}) for cid in child_ids_sorted]
            node["unread_count"] += sum(c["unread_count"] for c in node["children"])
            node["mention_count"] += sum(c["mention_count"] for c in node["children"])
        return node

    root_ids = sorted((rid for rid in rooms if rid not in child_ids), key=sort_key)
    tree = [build_node(rid, frozenset()) for rid in root_ids]

    rendered: set[str] = set()

    def collect(nodes):
        for n in nodes:
            rendered.add(n["room_id"])
            collect(n["children"])

    collect(tree)

    # A room can end up with no valid root at all if it's caught in a cycle
    # with nothing else anchoring that cycle to a real root (shouldn't
    # happen with genuine Matrix data, but silently dropping a room from
    # this view entirely would be a worse failure than showing it twice) -
    # anything never rendered above gets added as its own fallback root.
    # Checked one at a time (not precomputed as a single list) since an
    # earlier fallback root's own recursion can sweep in a later one first.
    for room_id in sorted(rooms, key=sort_key):
        if room_id in rendered:
            continue
        node = build_node(room_id, frozenset())
        tree.append(node)
        collect([node])

    return tree


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

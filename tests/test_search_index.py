from app import search_index


def add(conn, event_id, room_id, room_name, sender, body, ts):
    search_index.add_message(conn, event_id, room_id, room_name, sender, body, ts)


def test_search_escapes_terms_and_applies_filters_and_sort(search_conn):
    add(search_conn, "e1", "r1", "General", "@alice:test", 'hello "world"', 100)
    add(search_conn, "e2", "r1", "General", "@bob:test", "hello world again", 300)
    add(search_conn, "e3", "r2", "Random", "@bob:test", "hello world", 200)

    newest = search_index.search(search_conn, 'hello "world"', room_id="r1", since_ts=50, sort="newest")
    oldest = search_index.search(search_conn, "hello world", sort="oldest", limit=2)

    assert [row["event_id"] for row in newest] == ["e2", "e1"]
    assert [row["event_id"] for row in oldest] == ["e1", "e3"]
    assert "[[" in newest[0]["snippet"]


def test_add_ignores_empty_body_and_reports_stats_and_rooms(search_conn):
    search_index.add_message(search_conn, "empty", "r1", "General", "@alice:test", "", 1)
    add(search_conn, "e1", "r1", "General", "@alice:test", "one", 1)
    add(search_conn, "e2", "r2", None, "@alice:test", "two", 2)

    assert search_index.get_stats(search_conn) == {"indexed_messages": 2, "rooms": 2}
    assert {room["room_id"]: room["room_name"] for room in search_index.list_rooms(search_conn)} == {
        "r1": "General",
        "r2": "r2",
    }


def test_prune_and_clear_remove_messages_and_fts_rows(search_conn):
    add(search_conn, "old", "r1", "General", "@alice:test", "old message", 100)
    add(search_conn, "new", "r1", "General", "@alice:test", "new message", 200)

    assert search_index.prune_older_than(search_conn, 150) == 1
    assert search_index.search(search_conn, "old") == []
    assert search_index.clear_all(search_conn) == 1
    assert search_index.get_stats(search_conn)["indexed_messages"] == 0


def test_room_upsert_and_read_marker_only_move_forward(search_conn):
    search_index.upsert_room(search_conn, "r1", "General", False, "mxc://one")
    search_index.upsert_room(search_conn, "r1", "Renamed", True, "mxc://two")
    search_index.update_read_marker(search_conn, "r1", 200)
    search_index.update_read_marker(search_conn, "r1", 100)

    assert search_conn.execute(
        "SELECT room_name, is_direct, avatar_mxc, read_marker_ts FROM rooms WHERE room_id='r1'"
    ).fetchone() == ("Renamed", 1, "mxc://two", 200)


def test_unread_counts_exclude_self_and_match_mentions_as_whole_words(search_conn):
    search_index.upsert_room(search_conn, "r1", "General", False)
    search_index.update_read_marker(search_conn, "r1", 100)
    add(search_conn, "before", "r1", "General", "@bob:test", "@alice old", 90)
    add(search_conn, "self", "r1", "General", "@alice:test", "@alice self", 110)
    add(search_conn, "mention", "r1", "General", "@bob:test", "hello alice and @room", 120)
    add(search_conn, "substring", "r1", "General", "@bob:test", "malice is not a mention", 130)
    add(search_conn, "unknown", "r2", "Unknown", "@bob:test", "alice", 140)

    assert search_index.unread_counts_by_room(search_conn, "@alice:test") == {
        "r1": {"unread_count": 2, "mention_count": 1}
    }


def test_recent_and_last_messages_preserve_room_metadata(search_conn):
    search_index.upsert_room(search_conn, "dm", "Alice", True, "mxc://alice")
    search_index.upsert_room(search_conn, "room", "General", False)
    add(search_conn, "d1", "dm", "Alice", "@alice:test", "older", 100)
    add(search_conn, "d2", "dm", "Alice", "@alice:test", "latest dm", 300)
    add(search_conn, "r1", "room", "General", "@bob:test", "latest room", 200)

    recent = search_index.recent_conversations(search_conn, limit=1)
    last = search_index.last_message_by_room(search_conn)

    assert recent["direct"][0]["body"] == "latest dm"
    assert recent["rooms"][0]["body"] == "latest room"
    assert last["dm"]["is_direct"] is True
    assert last["dm"]["avatar_mxc"] == "mxc://alice"

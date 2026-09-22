import sqlite3

from app import control_store


def test_init_migrates_legacy_users_table(tmp_path):
    path = tmp_path / "control.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE users (user_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, created_at REAL NOT NULL)")
    conn.commit()
    conn.close()

    migrated = control_store.init_db(str(path))
    columns = {row[1] for row in migrated.execute("PRAGMA table_info(users)")}

    assert {"last_known_messages", "last_known_rooms"} <= columns


def test_user_crud_and_cached_totals():
    conn = control_store.init_db(":memory:")
    control_store.upsert_user(conn, "@alice:test", "A")
    control_store.upsert_user(conn, "@bob:test", "B")
    control_store.upsert_user(conn, "@alice:test", "A2")

    assert control_store.get_user(conn, "@alice:test") == {"user_id": "@alice:test", "device_id": "A2"}
    assert control_store.get_user(conn, "@missing:test") is None
    assert control_store.total_user_count(conn) == 2

    control_store.update_user_stats_cache(conn, "@alice:test", 10, 2)
    control_store.update_user_stats_cache(conn, "@bob:test", 5, 1)
    assert control_store.total_indexed_stats(conn) == {"messages": 15, "rooms": 3}
    assert {user["user_id"] for user in control_store.all_users(conn)} == {"@alice:test", "@bob:test"}

    control_store.delete_user(conn, "@bob:test")
    assert control_store.total_user_count(conn) == 1


def test_dynamic_admin_crud():
    conn = control_store.init_db(":memory:")
    control_store.add_admin(conn, "@alice:test", "@owner:test")
    control_store.add_admin(conn, "@alice:test", "@someone-else:test")

    assert control_store.is_dynamic_admin(conn, "@alice:test")
    assert control_store.list_admins(conn)[0]["added_by"] == "@owner:test"

    control_store.remove_admin(conn, "@alice:test")
    assert not control_store.is_dynamic_admin(conn, "@alice:test")


def test_metrics_accumulate_by_day_and_all_time(monkeypatch):
    conn = control_store.init_db(":memory:")
    monkeypatch.setattr(control_store, "_today", lambda: "2026-09-22")
    control_store.increment_metric(conn, "searches")
    control_store.increment_metric(conn, "searches", 4)
    conn.execute("INSERT INTO daily_metrics VALUES ('2026-09-21', 'searches', 3)")
    conn.commit()

    assert control_store.metric_today(conn, "searches") == 5
    assert control_store.metric_all_time(conn, "searches") == 8
    assert control_store.metric_today(conn, "missing") == 0
    assert control_store.metric_all_time(conn, "missing") == 0

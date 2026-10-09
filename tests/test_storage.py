import sqlite3
import pytest
from schedule_bot import storage


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    yield connection
    connection.close()


def test_ensure_default_settings_only_fills_missing_keys(conn):
    storage.set_setting(conn, "group_name", "Б-Мв-101")
    storage.ensure_default_settings(conn, {"group_name": "SHOULD_NOT_OVERWRITE", "daily_send_time": "09:00"})
    assert storage.get_setting(conn, "group_name") == "Б-Мв-101"
    assert storage.get_setting(conn, "daily_send_time") == "09:00"


def test_get_setting_returns_none_for_missing_key(conn):
    assert storage.get_setting(conn, "nope") is None


def test_set_setting_overwrites_existing_value(conn):
    storage.set_setting(conn, "check_interval_minutes", "60")
    storage.set_setting(conn, "check_interval_minutes", "30")
    assert storage.get_setting(conn, "check_interval_minutes") == "30"


def test_subscriber_lifecycle(conn):
    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    storage.add_subscriber(conn, 222, "Б-Мв-101", "2026-09-16T00:00:00")
    storage.add_subscriber(conn, 333, "OTHER-GROUP", "2026-09-16T00:00:00")
    assert sorted(storage.list_subscribers(conn, "Б-Мв-101")) == [111, 222]
    storage.remove_subscriber(conn, 111)
    assert storage.list_subscribers(conn, "Б-Мв-101") == [222]


def test_add_subscriber_is_idempotent(conn):
    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    assert storage.list_subscribers(conn, "Б-Мв-101") == [111]


def test_update_subscribers_group_migrates_matching_rows(conn):
    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    storage.add_subscriber(conn, 222, "Б-Мв-101", "2026-09-16T00:00:00")
    storage.add_subscriber(conn, 333, "OTHER-GROUP", "2026-09-16T00:00:00")

    migrated = storage.update_subscribers_group(conn, "Б-Мв-101", "Б-Мв-102")

    assert migrated == 2
    assert sorted(storage.list_subscribers(conn, "Б-Мв-102")) == [111, 222]
    assert storage.list_subscribers(conn, "Б-Мв-101") == []
    assert storage.list_subscribers(conn, "OTHER-GROUP") == [333]


def test_get_active_version_returns_none_when_empty(conn):
    assert storage.get_active_version(conn, "Б-Мв-101") is None


def test_insert_new_version_becomes_active_and_supersedes_old(conn):
    first_id = storage.insert_new_version(
        conn, "Б-Мв-101", '{"a": 1}', "/data/pdf_cache/v1.pdf", "2026-09-01T09:00:00"
    )
    second_id = storage.insert_new_version(
        conn, "Б-Мв-101", '{"a": 2}', "/data/pdf_cache/v2.pdf", "2026-09-08T09:00:00"
    )
    active = storage.get_active_version(conn, "Б-Мв-101")
    assert active.id == second_id
    assert active.payload_json == '{"a": 2}'
    assert active.status == "active"

    all_rows = conn.execute(
        "SELECT id, status FROM schedule_versions WHERE group_name = ? ORDER BY id", ("Б-Мв-101",)
    ).fetchall()
    assert all_rows == [(first_id, "superseded"), (second_id, "active")]


def test_versions_are_scoped_by_group_name(conn):
    storage.insert_new_version(conn, "Б-Мв-101", '{"a": 1}', "/v1.pdf", "2026-09-01T09:00:00")
    storage.insert_new_version(conn, "OTHER-GROUP", '{"b": 1}', "/v2.pdf", "2026-09-01T09:00:00")
    active = storage.get_active_version(conn, "Б-Мв-101")
    assert active.payload_json == '{"a": 1}'


def test_create_group_returns_new_group_with_defaults(conn):
    group_id = storage.create_group(
        conn,
        name="Б-Мв-101",
        source_page_url="https://example.com/schedules",
        pdf_source_url="https://example.com/101.pdf",
        send_time="09:00",
        check_interval_minutes=60,
        tz_name="Europe/Moscow",
    )
    group = storage.get_group(conn, group_id)
    assert group.name == "Б-Мв-101"
    assert group.pdf_source_url == "https://example.com/101.pdf"
    assert group.send_time == "09:00"
    assert group.check_interval_minutes == 60
    assert group.timezone == "Europe/Moscow"
    assert group.last_sync_failed_at is None


def test_create_group_rejects_duplicate_name(conn):
    storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_group(
            conn, name="Б-Мв-101", source_page_url="u2", pdf_source_url="p2",
            send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
        )


def test_get_group_returns_none_for_missing_id(conn):
    assert storage.get_group(conn, 999) is None


def test_get_group_by_name_returns_none_when_absent(conn):
    assert storage.get_group_by_name(conn, "Nope") is None


def test_list_groups_orders_alphabetically_and_paginates(conn):
    for name in ["В-группа", "А-группа", "Б-группа"]:
        storage.create_group(
            conn, name=name, source_page_url="u", pdf_source_url="p",
            send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
        )
    page1 = storage.list_groups(conn, limit=2, offset=0)
    page2 = storage.list_groups(conn, limit=2, offset=2)
    assert [g.name for g in page1] == ["А-группа", "Б-группа"]
    assert [g.name for g in page2] == ["В-группа"]
    assert storage.count_groups(conn) == 3


def test_list_all_groups_returns_every_group_unpaginated(conn):
    for i in range(10):
        storage.create_group(
            conn, name=f"Группа-{i}", source_page_url="u", pdf_source_url="p",
            send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
        )
    assert len(storage.list_all_groups(conn)) == 10


def test_update_group_pdf_source_url(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="old.pdf",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.update_group_pdf_source_url(conn, group_id, "new.pdf")
    assert storage.get_group(conn, group_id).pdf_source_url == "new.pdf"


def test_update_group_send_time_and_check_interval(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.update_group_send_time(conn, group_id, "07:30")
    storage.update_group_check_interval(conn, group_id, 30)
    group = storage.get_group(conn, group_id)
    assert group.send_time == "07:30"
    assert group.check_interval_minutes == 30


def test_set_group_last_sync_failed_at(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.set_group_last_sync_failed_at(conn, group_id, "2026-10-08T00:00:00+00:00")
    assert storage.get_group(conn, group_id).last_sync_failed_at == "2026-10-08T00:00:00+00:00"
    storage.set_group_last_sync_failed_at(conn, group_id, "")
    assert storage.get_group(conn, group_id).last_sync_failed_at == ""

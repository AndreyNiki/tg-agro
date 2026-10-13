import sqlite3
import json
import pytest
from schedule_bot import storage


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    yield connection
    connection.close()


def _make_institute(conn, name="ИТ"):
    return storage.create_institute(
        conn, name=name, catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )


def _make_link(conn, institute_id, pdf_url="https://example.com/a.pdf", group_names=None):
    return storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url=pdf_url,
        group_names=group_names or ["Б-Мв-101"], raw_label="Б-Мв-101", last_seen_at="t",
    )


def _make_group(conn, name="Б-Мв-101", institute_id=None, institute_link_id=None):
    if institute_id is None:
        institute_id = _make_institute(conn, f"ИТ-{name}")
    if institute_link_id is None:
        institute_link_id = _make_link(conn, institute_id, pdf_url=f"{name}.pdf", group_names=[name])
    return storage.create_group(conn, institute_id=institute_id, institute_link_id=institute_link_id, name=name)


def test_init_db_rebuilds_old_shape_groups_table(tmp_path):
    db_path = tmp_path / "test.db"
    connection = sqlite3.connect(str(db_path))
    connection.executescript(
        """
        CREATE TABLE groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            source_page_url TEXT NOT NULL,
            pdf_source_url TEXT NOT NULL,
            send_time TEXT NOT NULL,
            check_interval_minutes INTEGER NOT NULL,
            timezone TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO groups (name, source_page_url, pdf_source_url, send_time, check_interval_minutes, timezone, created_at)
        VALUES ('old-group', 'u', 'p', '09:00', 60, 'Europe/Moscow', 't');
        """
    )
    connection.commit()

    storage.init_db(connection)

    columns = {row[1] for row in connection.execute("PRAGMA table_info(groups)").fetchall()}
    assert "institute_id" in columns
    assert connection.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0


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


def test_get_active_version_returns_none_when_empty(conn):
    group_id = _make_group(conn)
    assert storage.get_active_version(conn, group_id) is None


def test_insert_new_version_becomes_active_and_supersedes_old(conn):
    group_id = _make_group(conn)
    first_id = storage.insert_new_version(
        conn, group_id, '{"a": 1}', "/data/pdf_cache/v1.pdf", "2026-09-01T09:00:00"
    )
    second_id = storage.insert_new_version(
        conn, group_id, '{"a": 2}', "/data/pdf_cache/v2.pdf", "2026-09-08T09:00:00"
    )
    active = storage.get_active_version(conn, group_id)
    assert active.id == second_id
    assert active.payload_json == '{"a": 2}'
    assert active.status == "active"

    all_rows = conn.execute(
        "SELECT id, status FROM schedule_versions WHERE group_id = ? ORDER BY id", (group_id,)
    ).fetchall()
    assert all_rows == [(first_id, "superseded"), (second_id, "active")]


def test_versions_are_scoped_by_group_id(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.insert_new_version(conn, group_a, '{"a": 1}', "/v1.pdf", "2026-09-01T09:00:00")
    storage.insert_new_version(conn, group_b, '{"b": 1}', "/v2.pdf", "2026-09-01T09:00:00")
    active = storage.get_active_version(conn, group_a)
    assert active.payload_json == '{"a": 1}'


def test_create_group_returns_new_group_with_defaults(conn):
    institute_id = _make_institute(conn)
    link_id = _make_link(conn, institute_id)
    group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Б-Мв-101")
    group = storage.get_group(conn, group_id)
    assert group.name == "Б-Мв-101"
    assert group.institute_id == institute_id
    assert group.institute_link_id == link_id
    assert group.is_verified == 0
    assert group.last_sync_failed_at is None


def test_create_group_rejects_duplicate_name_within_institute(conn):
    institute_id = _make_institute(conn)
    link_id = _make_link(conn, institute_id)
    storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Б-Мв-101")
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Б-Мв-101")


def test_create_group_allows_same_name_in_different_institutes(conn):
    institute_a = _make_institute(conn, "А")
    institute_b = _make_institute(conn, "Б")
    link_a = _make_link(conn, institute_a)
    link_b = _make_link(conn, institute_b)
    storage.create_group(conn, institute_id=institute_a, institute_link_id=link_a, name="Б-Мв-101")
    storage.create_group(conn, institute_id=institute_b, institute_link_id=link_b, name="Б-Мв-101")  # must not raise


def test_get_group_returns_none_for_missing_id(conn):
    assert storage.get_group(conn, 999) is None


def test_get_group_by_name_scoped_to_institute(conn):
    institute_a = _make_institute(conn, "А")
    institute_b = _make_institute(conn, "Б")
    link_a = _make_link(conn, institute_a)
    storage.create_group(conn, institute_id=institute_a, institute_link_id=link_a, name="Б-Мв-101")
    assert storage.get_group_by_name(conn, institute_a, "Б-Мв-101") is not None
    assert storage.get_group_by_name(conn, institute_b, "Б-Мв-101") is None


def test_list_groups_for_link_returns_only_that_links_groups(conn):
    institute_id = _make_institute(conn)
    link_a = _make_link(conn, institute_id, pdf_url="a.pdf", group_names=["A"])
    link_b = _make_link(conn, institute_id, pdf_url="b.pdf", group_names=["B"])
    storage.create_group(conn, institute_id=institute_id, institute_link_id=link_a, name="A")
    storage.create_group(conn, institute_id=institute_id, institute_link_id=link_b, name="B")
    assert [g.name for g in storage.list_groups_for_link(conn, link_a)] == ["A"]


def test_list_groups_orders_alphabetically_and_paginates(conn):
    institute_id = _make_institute(conn)
    for name in ["В-группа", "А-группа", "Б-группа"]:
        link_id = _make_link(conn, institute_id, pdf_url=f"{name}.pdf", group_names=[name])
        storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name=name)
    page1 = storage.list_groups(conn, institute_id, limit=2, offset=0)
    page2 = storage.list_groups(conn, institute_id, limit=2, offset=2)
    assert [g.name for g in page1] == ["А-группа", "Б-группа"]
    assert [g.name for g in page2] == ["В-группа"]
    assert storage.count_groups(conn, institute_id) == 3


def test_list_groups_excludes_unverified_minus_one_by_default(conn):
    institute_id = _make_institute(conn)
    link_id = _make_link(conn, institute_id)
    good = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Good")
    bad = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Bad")
    storage.update_group_is_verified(conn, bad, -1)
    names = [g.name for g in storage.list_groups(conn, institute_id, limit=10, offset=0)]
    assert names == ["Good"]
    assert storage.count_groups(conn, institute_id) == 1


def test_update_group_is_verified(conn):
    institute_id = _make_institute(conn)
    link_id = _make_link(conn, institute_id)
    group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="A")
    storage.update_group_is_verified(conn, group_id, 1)
    assert storage.get_group(conn, group_id).is_verified == 1


def test_update_group_institute_link_id_reassigns(conn):
    institute_id = _make_institute(conn)
    link_a = _make_link(conn, institute_id, pdf_url="a.pdf", group_names=["A"])
    link_b = _make_link(conn, institute_id, pdf_url="b.pdf", group_names=["A"])
    group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_a, name="A")
    storage.update_group_institute_link_id(conn, group_id, link_b)
    assert storage.get_group(conn, group_id).institute_link_id == link_b


def test_list_all_groups_returns_every_group_unpaginated(conn):
    for i in range(10):
        institute_id = _make_institute(conn, f"ИТ-{i}")
        link_id = _make_link(conn, institute_id, pdf_url=f"{i}.pdf", group_names=[f"Группа-{i}"])
        storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name=f"Группа-{i}")
    assert len(storage.list_all_groups(conn)) == 10


def test_set_group_last_sync_failed_at(conn):
    group_id = _make_group(conn)
    storage.set_group_last_sync_failed_at(conn, group_id, "2026-10-08T00:00:00+00:00")
    assert storage.get_group(conn, group_id).last_sync_failed_at == "2026-10-08T00:00:00+00:00"
    storage.set_group_last_sync_failed_at(conn, group_id, "")
    assert storage.get_group(conn, group_id).last_sync_failed_at == ""


def test_upsert_member_creates_new_member(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="2026-10-08T00:00:00")
    member = storage.get_member(conn, 111)
    assert member.group_id == group_id
    assert member.subscribed is True
    assert member.telegram_username is None


def test_upsert_member_moves_existing_chat_to_new_group(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="2026-10-08T00:00:00")
    storage.upsert_member(conn, 111, group_b, joined_at="2026-10-08T00:00:01")
    assert storage.get_member(conn, 111).group_id == group_b


def test_get_member_returns_none_when_absent(conn):
    assert storage.get_member(conn, 111) is None


def test_set_member_subscribed_toggles_without_losing_group(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="2026-10-08T00:00:00")
    storage.set_member_subscribed(conn, 111, False)
    member = storage.get_member(conn, 111)
    assert member.subscribed is False
    assert member.group_id == group_id
    storage.set_member_subscribed(conn, 111, True)
    assert storage.get_member(conn, 111).subscribed is True


def test_update_member_profile_sets_username_and_display_name(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="2026-10-08T00:00:00")
    storage.update_member_profile(conn, 111, telegram_username="ivan", display_name="Ivan Ivanov")
    member = storage.get_member(conn, 111)
    assert member.telegram_username == "ivan"
    assert member.display_name == "Ivan Ivanov"


def test_list_group_members_defaults_to_subscribed_only(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t", subscribed=True)
    storage.upsert_member(conn, 222, group_id, joined_at="t", subscribed=False)
    assert storage.list_group_members(conn, group_id) == [111]
    assert sorted(storage.list_group_members(conn, group_id, subscribed_only=False)) == [111, 222]


def test_list_members_missing_profile_returns_chats_without_username_or_name(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    storage.update_member_profile(conn, 222, telegram_username="ivan", display_name="Ivan")
    assert storage.list_members_missing_profile(conn) == [111]


def test_list_all_member_chat_ids_spans_every_group(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.upsert_member(conn, 222, group_b, joined_at="t", subscribed=False)
    assert sorted(storage.list_all_member_chat_ids(conn)) == [111, 222]


def test_create_group_starosta_fields_default_to_none(conn):
    group_id = _make_group(conn)
    group = storage.get_group(conn, group_id)
    assert group.starosta_chat_id is None
    assert group.last_starosta_change_at is None


def test_update_group_starosta_sets_and_clears(conn):
    group_id = _make_group(conn)
    storage.update_group_starosta(conn, group_id, 111)
    assert storage.get_group(conn, group_id).starosta_chat_id == 111
    storage.update_group_starosta(conn, group_id, None)
    assert storage.get_group(conn, group_id).starosta_chat_id is None


def test_set_group_last_starosta_change_at(conn):
    group_id = _make_group(conn)
    storage.set_group_last_starosta_change_at(conn, group_id, "2026-10-08T00:00:00+00:00")
    assert storage.get_group(conn, group_id).last_starosta_change_at == "2026-10-08T00:00:00+00:00"


def test_create_election_defaults_to_open(conn):
    group_id = _make_group(conn)
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "2026-10-08T00:00:00")
    election = storage.get_election(conn, election_id)
    assert election.status == "open"
    assert election.kind == "initial"
    assert election.candidate_chat_id == 111


def test_get_open_election_returns_none_when_absent(conn):
    group_id = _make_group(conn)
    assert storage.get_open_election(conn, group_id) is None


def test_get_open_election_finds_the_open_one(conn):
    group_id = _make_group(conn)
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")
    storage.close_election(conn, election_id, "cancelled", "t2")
    second_id = storage.create_election(conn, group_id, "change", 222, 333, "t3")
    open_election = storage.get_open_election(conn, group_id)
    assert open_election.id == second_id


def test_list_open_elections_is_scoped_by_group(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.create_election(conn, group_a, "initial", 111, 111, "t")
    storage.create_election(conn, group_b, "initial", 222, 222, "t")
    assert len(storage.list_open_elections(conn, group_a)) == 1


def test_close_election_updates_status_and_decided_at(conn):
    group_id = _make_group(conn)
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")
    storage.close_election(conn, election_id, "approved", "2026-10-08T01:00:00")
    election = storage.get_election(conn, election_id)
    assert election.status == "approved"
    assert election.decided_at == "2026-10-08T01:00:00"


def test_cancel_open_elections_for_group_only_affects_open_ones(conn):
    group_id = _make_group(conn)
    open_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")
    storage.cancel_open_elections_for_group(conn, group_id, "t2")
    assert storage.get_election(conn, open_id).status == "cancelled"
    assert storage.get_open_election(conn, group_id) is None


def test_add_election_approval_is_idempotent_per_chat_id(conn):
    group_id = _make_group(conn)
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")
    first = storage.add_election_approval(conn, election_id, 222, "t")
    second = storage.add_election_approval(conn, election_id, 222, "t2")
    assert first is True
    assert second is False
    assert storage.count_election_approvals(conn, election_id) == 1


def test_count_election_approvals_reaches_two_on_distinct_voters(conn):
    group_id = _make_group(conn)
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")
    storage.add_election_approval(conn, election_id, 222, "t")
    storage.add_election_approval(conn, election_id, 333, "t")
    assert storage.count_election_approvals(conn, election_id) == 2


def test_create_group_change_request_defaults_to_pending(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    request = storage.get_group_change_request(conn, request_id)
    assert request.status == "pending"
    assert request.from_group_id == group_a
    assert request.to_group_id == group_b


def test_create_group_change_request_accepts_null_from_group(conn):
    group_b = _make_group(conn, "B")
    request_id = storage.create_group_change_request(conn, 111, None, group_b, "t")
    request = storage.get_group_change_request(conn, request_id)
    assert request.from_group_id is None
    assert request.to_group_id == group_b


def test_migrate_group_change_requests_preserves_existing_rows(conn):
    """Simulates an upgrade from sub-project 2's NOT NULL schema: an
    existing row with a real from_group_id must survive the rebuild
    unchanged, including an already-decided one."""
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    conn.execute("DROP TABLE group_change_requests")
    conn.execute(
        "CREATE TABLE group_change_requests (id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, "
        "from_group_id INTEGER NOT NULL REFERENCES groups(id), to_group_id INTEGER NOT NULL REFERENCES groups(id), "
        "requested_at TEXT NOT NULL, status TEXT NOT NULL, decided_at TEXT)"
    )
    conn.execute(
        "INSERT INTO group_change_requests (chat_id, from_group_id, to_group_id, requested_at, status, decided_at) "
        "VALUES (111, ?, ?, 't', 'approved', 't2')",
        (group_a, group_b),
    )
    conn.commit()

    storage._migrate_group_change_requests_nullable_from_group(conn)

    rows = conn.execute("SELECT chat_id, from_group_id, to_group_id, status, decided_at FROM group_change_requests").fetchall()
    assert rows == [(111, group_a, group_b, "approved", "t2")]
    # and the column is now nullable:
    new_request_id = storage.create_group_change_request(conn, 222, None, group_b, "t3")
    assert storage.get_group_change_request(conn, new_request_id).from_group_id is None


def test_get_pending_request_for_chat_returns_none_when_absent(conn):
    assert storage.get_pending_request_for_chat(conn, 111) is None


def test_get_pending_request_for_chat_ignores_decided_requests(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    storage.decide_group_change_request(conn, request_id, "approved", "t2")
    assert storage.get_pending_request_for_chat(conn, 111) is None


def test_decide_group_change_request_updates_status(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    storage.decide_group_change_request(conn, request_id, "rejected", "t2")
    request = storage.get_group_change_request(conn, request_id)
    assert request.status == "rejected"
    assert request.decided_at == "t2"


def test_list_pending_requests_for_group_scoped_by_target(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.create_group_change_request(conn, 111, group_a, group_b, "t1")
    storage.create_group_change_request(conn, 222, group_b, group_a, "t2")
    pending_for_b = storage.list_pending_requests_for_group(conn, group_b)
    assert [r.chat_id for r in pending_for_b] == [111]


def test_has_group_exclusion_false_when_none_recorded(conn):
    group_id = _make_group(conn)
    assert storage.has_group_exclusion(conn, group_id, 111) is False


def test_create_group_exclusion_then_has_exclusion_true(conn):
    group_id = _make_group(conn)
    storage.create_group_exclusion(conn, group_id, 111, "t", 999)
    assert storage.has_group_exclusion(conn, group_id, 111) is True


def test_group_exclusion_is_scoped_by_group(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.create_group_exclusion(conn, group_a, 111, "t", 999)
    assert storage.has_group_exclusion(conn, group_b, 111) is False


def test_remove_member_deletes_the_row(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.remove_member(conn, 111)
    assert storage.get_member(conn, 111) is None


def test_create_group_defaults_to_open(conn):
    group_id = _make_group(conn)
    assert storage.get_group(conn, group_id).is_closed is False


def test_update_group_closed_toggles(conn):
    group_id = _make_group(conn)
    storage.update_group_closed(conn, group_id, True)
    assert storage.get_group(conn, group_id).is_closed is True
    storage.update_group_closed(conn, group_id, False)
    assert storage.get_group(conn, group_id).is_closed is False


def test_get_group_where_starosta_returns_none_when_nobody_is(conn):
    _make_group(conn)
    assert storage.get_group_where_starosta(conn, 111) is None


def test_get_group_where_starosta_finds_the_right_group(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.update_group_starosta(conn, group_b, 111)
    found = storage.get_group_where_starosta(conn, 111)
    assert found.id == group_b


def test_create_institute_returns_row_with_defaults(conn):
    institute_id = storage.create_institute(
        conn, name="ИТ", catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )
    institute = storage.get_institute(conn, institute_id)
    assert institute.name == "ИТ"
    assert institute.catalog_url == "https://example.com/catalog"
    assert institute.last_catalog_sync_at is None


def test_create_institute_rejects_duplicate_name(conn):
    storage.create_institute(
        conn, name="ИТ", catalog_url="u", check_interval_minutes=60,
        send_time="09:00", tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_institute(
            conn, name="ИТ", catalog_url="u2", check_interval_minutes=60,
            send_time="09:00", tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
        )


def test_list_institutes_orders_alphabetically(conn):
    storage.create_institute(conn, name="Б", catalog_url="u", check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080)
    storage.create_institute(conn, name="А", catalog_url="u", check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080)
    assert [i.name for i in storage.list_institutes(conn)] == ["А", "Б"]


def test_update_institute_fields(conn):
    institute_id = _make_institute(conn)
    storage.update_institute_catalog_url(conn, institute_id, "https://new.example.com")
    storage.update_institute_check_interval(conn, institute_id, 30)
    storage.update_institute_send_time(conn, institute_id, "08:00")
    storage.update_institute_catalog_refresh_interval(conn, institute_id, 1440)
    institute = storage.get_institute(conn, institute_id)
    assert institute.catalog_url == "https://new.example.com"
    assert institute.check_interval_minutes == 30
    assert institute.send_time == "08:00"
    assert institute.catalog_refresh_interval_minutes == 1440


def test_set_institute_last_catalog_sync_at(conn):
    institute_id = _make_institute(conn)
    storage.set_institute_last_catalog_sync_at(conn, institute_id, "2026-10-09T00:00:00+00:00")
    assert storage.get_institute(conn, institute_id).last_catalog_sync_at == "2026-10-09T00:00:00+00:00"


def test_get_institute_returns_none_for_missing_id(conn):
    assert storage.get_institute(conn, 999) is None


def test_create_institute_link_round_trips_group_names(conn):
    institute_id = _make_institute(conn)
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url="https://example.com/a.pdf",
        group_names=["Б-А-301", "Б-А-302"], raw_label="Б-А-301,302", last_seen_at="t",
    )
    link = storage.get_institute_link(conn, link_id)
    assert link.pdf_url == "https://example.com/a.pdf"
    assert json.loads(link.group_names_json) == ["Б-А-301", "Б-А-302"]
    assert link.is_stale is False


def test_create_institute_link_rejects_duplicate_pdf_url_for_same_institute(conn):
    institute_id = _make_institute(conn)
    storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u", group_names=["A"], raw_label="A", last_seen_at="t")
    with pytest.raises(sqlite3.IntegrityError):
        storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u", group_names=["B"], raw_label="B", last_seen_at="t")


def test_get_institute_link_by_pdf_url_scoped_to_institute(conn):
    institute_a = _make_institute(conn, "А")
    institute_b = _make_institute(conn, "Б")
    storage.create_institute_link(conn, institute_id=institute_a, pdf_url="u", group_names=["A"], raw_label="A", last_seen_at="t")
    assert storage.get_institute_link_by_pdf_url(conn, institute_b, "u") is None
    assert storage.get_institute_link_by_pdf_url(conn, institute_a, "u") is not None


def test_list_institute_links_includes_stale_by_default(conn):
    institute_id = _make_institute(conn)
    link_id = storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u", group_names=["A"], raw_label="A", last_seen_at="t")
    storage.mark_stale_links_not_in(conn, institute_id, confirmed_link_ids=[])
    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert links[0].is_stale is True
    assert storage.list_institute_links(conn, institute_id, include_stale=False) == []


def test_update_institute_link_group_names_resets_stale_and_bumps_last_seen(conn):
    institute_id = _make_institute(conn)
    link_id = storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u", group_names=["A"], raw_label="A", last_seen_at="t1")
    storage.mark_stale_links_not_in(conn, institute_id, confirmed_link_ids=[])
    storage.update_institute_link_group_names(conn, link_id, group_names=["A", "B"], raw_label="A,B", last_seen_at="t2")
    link = storage.get_institute_link(conn, link_id)
    assert json.loads(link.group_names_json) == ["A", "B"]
    assert link.raw_label == "A,B"
    assert link.last_seen_at == "t2"
    assert link.is_stale is False


def test_update_institute_link_pdf_url_keeps_id_stable(conn):
    institute_id = _make_institute(conn)
    link_id = storage.create_institute_link(conn, institute_id=institute_id, pdf_url="old.pdf", group_names=["A"], raw_label="A", last_seen_at="t")
    storage.update_institute_link_pdf_url(conn, link_id, "new.pdf")
    link = storage.get_institute_link(conn, link_id)
    assert link.id == link_id
    assert link.pdf_url == "new.pdf"


def test_mark_institute_link_seen_resets_stale_without_touching_names(conn):
    institute_id = _make_institute(conn)
    link_id = storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u", group_names=["A"], raw_label="A", last_seen_at="t1")
    storage.mark_stale_links_not_in(conn, institute_id, confirmed_link_ids=[])
    storage.mark_institute_link_seen(conn, link_id, "t2")
    link = storage.get_institute_link(conn, link_id)
    assert link.is_stale is False
    assert link.last_seen_at == "t2"
    assert json.loads(link.group_names_json) == ["A"]


def test_mark_stale_links_not_in_preserves_confirmed_ones(conn):
    institute_id = _make_institute(conn)
    kept = storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u1", group_names=["A"], raw_label="A", last_seen_at="t")
    dropped = storage.create_institute_link(conn, institute_id=institute_id, pdf_url="u2", group_names=["B"], raw_label="B", last_seen_at="t")
    storage.mark_stale_links_not_in(conn, institute_id, confirmed_link_ids=[kept])
    assert storage.get_institute_link(conn, kept).is_stale is False
    assert storage.get_institute_link(conn, dropped).is_stale is True


def test_get_institute_by_catalog_url_finds_existing(conn):
    institute_id = storage.create_institute(
        conn, name="Инст", catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )
    found = storage.get_institute_by_catalog_url(conn, "https://example.com/catalog")
    assert found is not None
    assert found.id == institute_id


def test_get_institute_by_catalog_url_returns_none_when_absent(conn):
    assert storage.get_institute_by_catalog_url(conn, "https://example.com/nope") is None


def test_reassign_member_group_updates_group_id_only(conn):
    institute_id = storage.create_institute(
        conn, name="Инст", catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url="https://example.com/a.pdf",
        group_names=["А", "Б"], raw_label="А,Б", last_seen_at="t",
    )
    old_group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="А")
    new_group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Б")
    storage.upsert_member(conn, 555, old_group_id, joined_at="2026-01-01T00:00:00+00:00", subscribed=False)
    storage.update_member_profile(conn, 555, telegram_username="bob", display_name="Bob")

    storage.reassign_member_group(conn, 555, new_group_id)

    member = storage.get_member(conn, 555)
    assert member.group_id == new_group_id
    assert member.subscribed is False  # untouched
    assert member.joined_at == "2026-01-01T00:00:00+00:00"  # untouched
    assert member.telegram_username == "bob"  # untouched

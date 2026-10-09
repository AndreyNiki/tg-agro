import sqlite3
import pytest
from schedule_bot import storage


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    yield connection
    connection.close()


def _make_group(conn, name="Б-Мв-101"):
    return storage.create_group(
        conn, name=name, source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )


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


def _seed_legacy_single_group_db(conn, *, with_schedule_version=False):
    """Recreates the pre-multi-group state this migration must handle:
    a `settings`-driven single group plus old-shape `subscribers` rows,
    and optionally an old-shape `schedule_versions` row that predates the
    group_id column (simulating an un-migrated production database)."""
    storage.set_setting(conn, "group_name", "Б-Мв-101")
    storage.set_setting(conn, "pdf_source", "https://example.com/101.pdf")
    storage.set_setting(conn, "daily_send_time", "09:00")
    storage.set_setting(conn, "check_interval_minutes", "60")
    storage.set_setting(conn, "timezone", "Europe/Moscow")
    conn.execute(
        "INSERT INTO subscribers (chat_id, group_name, joined_at) VALUES (?, ?, ?)",
        (111, "Б-Мв-101", "2026-09-16T00:00:00"),
    )
    conn.execute(
        "INSERT INTO subscribers (chat_id, group_name, joined_at) VALUES (?, ?, ?)",
        (222, "Б-Мв-101", "2026-09-16T00:00:00"),
    )
    conn.commit()
    if with_schedule_version:
        # Simulate a database created before Task 3's schema change: no
        # group_id column exists yet on schedule_versions.
        conn.execute("DROP TABLE schedule_versions")
        conn.execute(
            "CREATE TABLE schedule_versions (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "group_name TEXT NOT NULL, payload_json TEXT NOT NULL, pdf_cache_path TEXT NOT NULL, "
            "parsed_at TEXT NOT NULL, status TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO schedule_versions (group_name, payload_json, pdf_cache_path, parsed_at, status) "
            "VALUES ('Б-Мв-101', '{\"a\": 1}', '/v1.pdf', '2026-09-16T00:00:00', 'active')"
        )
        conn.commit()


def test_migrate_legacy_single_group_creates_group_from_settings(conn):
    _seed_legacy_single_group_db(conn)
    group_id = storage.migrate_legacy_single_group(conn, "https://example.com/schedules")
    group = storage.get_group(conn, group_id)
    assert group.name == "Б-Мв-101"
    assert group.source_page_url == "https://example.com/schedules"
    assert group.pdf_source_url == "https://example.com/101.pdf"
    assert group.send_time == "09:00"
    assert group.check_interval_minutes == 60
    assert group.timezone == "Europe/Moscow"


def test_migrate_legacy_single_group_moves_subscribers_to_members(conn):
    _seed_legacy_single_group_db(conn)
    group_id = storage.migrate_legacy_single_group(conn, "https://example.com/schedules")
    assert sorted(storage.list_group_members(conn, group_id)) == [111, 222]


def test_migrate_legacy_single_group_requires_source_page_url(conn):
    _seed_legacy_single_group_db(conn)
    with pytest.raises(ValueError):
        storage.migrate_legacy_single_group(conn, None)


def test_migrate_legacy_single_group_is_noop_on_fresh_install(conn):
    assert storage.migrate_legacy_single_group(conn, None) is None
    assert storage.count_groups(conn) == 0


def test_migrate_legacy_single_group_is_noop_when_already_migrated(conn):
    _seed_legacy_single_group_db(conn)
    first_id = storage.migrate_legacy_single_group(conn, "https://example.com/schedules")
    second_result = storage.migrate_legacy_single_group(conn, "https://example.com/schedules")
    assert second_result is None
    assert storage.count_groups(conn) == 1
    assert first_id is not None


def test_migrate_legacy_single_group_backfills_group_id_on_old_schedule_versions(conn):
    _seed_legacy_single_group_db(conn, with_schedule_version=True)
    group_id = storage.migrate_legacy_single_group(conn, "https://example.com/schedules")
    active = storage.get_active_version(conn, group_id)
    assert active is not None
    assert active.payload_json == '{"a": 1}'


def test_migrate_legacy_single_group_rebuilds_schedule_versions_so_inserts_work(conn):
    # Regression test for the critical bug: on a real pre-existing database,
    # the old group_name TEXT NOT NULL column survived a naive ALTER TABLE
    # ADD COLUMN migration, so the first insert_new_version call after
    # migration (i.e. the first time the schedule actually changes) raised
    # sqlite3.IntegrityError: NOT NULL constraint failed: schedule_versions.group_name.
    _seed_legacy_single_group_db(conn, with_schedule_version=True)
    group_id = storage.migrate_legacy_single_group(conn, "https://example.com/schedules")

    new_id = storage.insert_new_version(conn, group_id, '{"a": 2}', "/v2.pdf", "2026-10-09T00:00:00")

    assert new_id is not None
    active = storage.get_active_version(conn, group_id)
    assert active is not None
    assert active.payload_json == '{"a": 2}'
    assert active.pdf_cache_path == "/v2.pdf"

    # The old 'active' row from the legacy seed must now be superseded —
    # there must be exactly one active row left for this group_id.
    active_rows = conn.execute(
        "SELECT COUNT(*) FROM schedule_versions WHERE group_id = ? AND status = 'active'",
        (group_id,),
    ).fetchone()[0]
    assert active_rows == 1

    # The schedule_versions table must no longer have a group_name column
    # at all — it should match the current, final schema.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(schedule_versions)").fetchall()}
    assert "group_name" not in columns
    assert "group_id" in columns


def test_migrate_legacy_single_group_dedups_multiple_active_rows_onto_latest(conn):
    _seed_legacy_single_group_db(conn, with_schedule_version=True)
    # Simulate a second, older-shaped 'active' row that also predates the
    # group_id column (e.g. a stale row left behind by some historical bug)
    # so that after migration two rows would both claim status='active'
    # for the same group_id if not deduped.
    conn.execute(
        "INSERT INTO schedule_versions (group_name, payload_json, pdf_cache_path, parsed_at, status) "
        "VALUES ('Б-Мв-101', '{\"a\": 0}', '/v0.pdf', '2026-09-15T00:00:00', 'active')"
    )
    conn.commit()

    group_id = storage.migrate_legacy_single_group(conn, "https://example.com/schedules")

    active_rows = conn.execute(
        "SELECT payload_json FROM schedule_versions WHERE group_id = ? AND status = 'active'",
        (group_id,),
    ).fetchall()
    assert len(active_rows) == 1
    # The row with the later parsed_at ('2026-09-16...') must be the one
    # that stayed active.
    assert active_rows[0][0] == '{"a": 1}'


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

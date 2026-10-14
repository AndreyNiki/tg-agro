import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.config import Config
from schedule_bot.legacy_migration import run_legacy_migration_if_configured

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "schedule_sample.pdf"


@pytest.fixture
def conn():
    # check_same_thread=False: run_legacy_migration_if_configured runs
    # refresh_institute_catalog via asyncio.to_thread, like production
    # (main.py) — see tests/test_sync.py for the same pattern.
    connection = sqlite3.connect(":memory:", check_same_thread=False)
    storage.init_db(connection)
    return connection


def _config(**overrides):
    base = dict(
        telegram_token="t", admin_chat_id=1, db_path=":memory:", pdf_cache_dir="/tmp/cache",
        default_weektype_url="https://example.com/week", default_daily_send_time="09:00",
        default_check_interval_minutes=60, default_timezone="Europe/Moscow",
        default_catalog_refresh_interval_minutes=10080,
        legacy_migration_catalog_url="https://example.com/institut",
        legacy_migration_institute_name="Институт агробизнеса",
        legacy_migration_group_name="Б-Мв-101",
    )
    base.update(overrides)
    return Config(**base)


@pytest.mark.asyncio
async def test_noop_when_not_configured(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    monkeypatch.setattr(mod, "refresh_institute_catalog", AsyncMock())
    config = _config(legacy_migration_group_name=None)
    notify_admin = AsyncMock()
    reschedule = MagicMock()

    await run_legacy_migration_if_configured(conn, config, notify_admin, AsyncMock(), reschedule)

    notify_admin.assert_not_awaited()
    reschedule.assert_not_called()
    assert storage.get_setting(conn, "legacy_migration_done_at") is None


@pytest.mark.asyncio
async def test_noop_when_already_done(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    storage.set_setting(conn, "legacy_migration_done_at", "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(mod, "refresh_institute_catalog", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    config = _config()
    notify_admin = AsyncMock()
    reschedule = MagicMock()

    await run_legacy_migration_if_configured(conn, config, notify_admin, AsyncMock(), reschedule)

    notify_admin.assert_not_awaited()
    reschedule.assert_not_called()


@pytest.mark.asyncio
async def test_creates_institute_when_not_found_by_catalog_url(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    def fake_refresh(conn_, institute, *, notify_admin):
        link_id = storage.create_institute_link(
            conn_, institute_id=institute.id, pdf_url="https://example.com/a.pdf",
            group_names=["Б-Мв-101"], raw_label="Б-Мв-101", last_seen_at="t",
        )
        storage.create_group(conn_, institute_id=institute.id, institute_link_id=link_id, name="Б-Мв-101")

    monkeypatch.setattr(mod, "refresh_institute_catalog", fake_refresh)
    monkeypatch.setattr(mod, "validate_and_sync_group_on_first_subscribe", AsyncMock(return_value=True))
    config = _config()
    notify_admin = AsyncMock()
    reschedule = MagicMock()

    await run_legacy_migration_if_configured(conn, config, notify_admin, AsyncMock(), reschedule)

    institute = storage.get_institute_by_catalog_url(conn, "https://example.com/institut")
    assert institute is not None
    assert institute.name == "Институт агробизнеса"
    reschedule.assert_called_once_with(institute.id)


@pytest.mark.asyncio
async def test_reuses_existing_institute_found_by_catalog_url(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    existing_id = storage.create_institute(
        conn, name="Уже есть", catalog_url="https://example.com/institut",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )

    def fake_refresh(conn_, institute, *, notify_admin):
        assert institute.id == existing_id
        link_id = storage.create_institute_link(
            conn_, institute_id=institute.id, pdf_url="https://example.com/a.pdf",
            group_names=["Б-Мв-101"], raw_label="Б-Мв-101", last_seen_at="t",
        )
        storage.create_group(conn_, institute_id=institute.id, institute_link_id=link_id, name="Б-Мв-101")

    monkeypatch.setattr(mod, "refresh_institute_catalog", fake_refresh)
    monkeypatch.setattr(mod, "validate_and_sync_group_on_first_subscribe", AsyncMock(return_value=True))
    config = _config()

    await run_legacy_migration_if_configured(conn, config, AsyncMock(), AsyncMock(), MagicMock())

    assert len(storage.list_institutes(conn)) == 1  # no duplicate created


@pytest.mark.asyncio
async def test_postpones_when_group_not_found_after_scan(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    def fake_refresh(conn_, institute, *, notify_admin):
        pass  # catalog scan finds nothing matching the configured group name

    monkeypatch.setattr(mod, "refresh_institute_catalog", fake_refresh)
    validate_mock = AsyncMock()
    monkeypatch.setattr(mod, "validate_and_sync_group_on_first_subscribe", validate_mock)
    config = _config()
    notify_admin = AsyncMock()

    await run_legacy_migration_if_configured(conn, config, notify_admin, AsyncMock(), MagicMock())

    validate_mock.assert_not_awaited()
    notify_admin.assert_awaited_once()
    assert storage.get_setting(conn, "legacy_migration_done_at") is None


@pytest.mark.asyncio
async def test_postpones_when_pdf_validation_fails(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    def fake_refresh(conn_, institute, *, notify_admin):
        link_id = storage.create_institute_link(
            conn_, institute_id=institute.id, pdf_url="https://example.com/a.pdf",
            group_names=["Б-Мв-101"], raw_label="Б-Мв-101", last_seen_at="t",
        )
        storage.create_group(conn_, institute_id=institute.id, institute_link_id=link_id, name="Б-Мв-101")

    monkeypatch.setattr(mod, "refresh_institute_catalog", fake_refresh)
    monkeypatch.setattr(mod, "validate_and_sync_group_on_first_subscribe", AsyncMock(return_value=False))
    config = _config()
    notify_admin = AsyncMock()

    other_institute_id = storage.create_institute(
        conn, name="Другой", catalog_url="https://other.example.com",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )
    other_link_id = storage.create_institute_link(
        conn, institute_id=other_institute_id, pdf_url="https://other.example.com/x.pdf",
        group_names=["Старая"], raw_label="Старая", last_seen_at="t",
    )
    old_group_id = storage.create_group(conn, institute_id=other_institute_id, institute_link_id=other_link_id, name="Старая")
    storage.upsert_member(conn, 777, old_group_id, joined_at="t")

    await run_legacy_migration_if_configured(conn, config, notify_admin, AsyncMock(), MagicMock())

    notify_admin.assert_awaited_once()
    assert storage.get_setting(conn, "legacy_migration_done_at") is None
    assert storage.get_member(conn, 777).group_id == old_group_id  # untouched — validation never succeeded


@pytest.mark.asyncio
async def test_postpones_with_specific_message_when_group_already_failed_validation(conn, monkeypatch):
    """Once is_verified is already -1 (set by an earlier failed
    validation attempt), validate_and_sync_group_on_first_subscribe
    short-circuits WITHOUT any network call or its own admin alert (see
    its docstring: "Already-verified groups (1 or -1) skip this
    entirely"). The migration's own alert must mention that this group
    already failed PDF validation, not repeat a bare generic message
    that gives the admin no hint about the cause on every restart."""
    import schedule_bot.legacy_migration as mod

    def fake_refresh(conn_, institute, *, notify_admin):
        link_id = storage.create_institute_link(
            conn_, institute_id=institute.id, pdf_url="https://example.com/a.pdf",
            group_names=["Б-Мв-101"], raw_label="Б-Мв-101", last_seen_at="t",
        )
        group_id = storage.create_group(conn_, institute_id=institute.id, institute_link_id=link_id, name="Б-Мв-101")
        storage.update_group_is_verified(conn_, group_id, -1)

    monkeypatch.setattr(mod, "refresh_institute_catalog", fake_refresh)
    monkeypatch.setattr(mod, "validate_and_sync_group_on_first_subscribe", AsyncMock(return_value=False))
    config = _config()
    notify_admin = AsyncMock()

    await run_legacy_migration_if_configured(conn, config, notify_admin, AsyncMock(), MagicMock())

    notify_admin.assert_awaited_once()
    alert_text = notify_admin.await_args.args[0]
    assert "is_verified=-1" in alert_text or "ранее не подтвердилась" in alert_text
    assert storage.get_setting(conn, "legacy_migration_done_at") is None


@pytest.mark.asyncio
async def test_successful_migration_reassigns_all_members_and_sets_flag_once(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    def fake_refresh(conn_, institute, *, notify_admin):
        link_id = storage.create_institute_link(
            conn_, institute_id=institute.id, pdf_url="https://example.com/a.pdf",
            group_names=["Б-Мв-101"], raw_label="Б-Мв-101", last_seen_at="t",
        )
        storage.create_group(conn_, institute_id=institute.id, institute_link_id=link_id, name="Б-Мв-101")

    monkeypatch.setattr(mod, "refresh_institute_catalog", fake_refresh)
    monkeypatch.setattr(mod, "validate_and_sync_group_on_first_subscribe", AsyncMock(return_value=True))
    config = _config()
    notify_admin = AsyncMock()
    sync_fn = AsyncMock()
    reschedule = MagicMock()

    # Two chat_ids backed up from a destructive schema rebuild (see
    # storage._backup_legacy_member_chat_ids) — they have no current
    # `members` row of their own (lost in the wipe), which is exactly why
    # run_legacy_migration_if_configured must source its candidates from
    # legacy_members_backup, not list_all_member_chat_ids.
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS legacy_members_backup (chat_id INTEGER PRIMARY KEY);"
        "INSERT INTO legacy_members_backup (chat_id) VALUES (111), (222);"
    )
    conn.commit()

    await run_legacy_migration_if_configured(conn, config, notify_admin, sync_fn, reschedule)

    institute = storage.get_institute_by_catalog_url(conn, "https://example.com/institut")
    target_group = storage.get_group_by_name(conn, institute.id, "Б-Мв-101")

    member_1 = storage.get_member(conn, 111)
    member_2 = storage.get_member(conn, 222)
    assert member_1.group_id == target_group.id
    assert member_1.subscribed is True
    assert member_2.group_id == target_group.id
    assert member_2.subscribed is True

    assert storage.get_setting(conn, "legacy_migration_done_at") is not None
    notify_admin.assert_awaited_once()
    # 2 migrated chat_ids -> correct Russian plural ("участника", not
    # "участников" or "участник").
    assert "2 участника переключено" in notify_admin.await_args.args[0]

    # The immediate sync must run AFTER members are reassigned — at the
    # moment validate_and_sync_group_on_first_subscribe (mocked above) is
    # called, the target group has no members row yet, so
    # sync_institute_link_fn only does useful work once called again
    # here, post-reassignment (see group_commands.py's documented
    # members-before-sync ordering, reused here for the same reason).
    sync_fn.assert_awaited_once_with(target_group.institute_link_id)

    # Second call must be a pure no-op now that the flag is set.
    notify_admin.reset_mock()
    monkeypatch.setattr(mod, "refresh_institute_catalog", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run again")))
    await run_legacy_migration_if_configured(conn, config, notify_admin, sync_fn, reschedule)
    notify_admin.assert_not_awaited()


@pytest.mark.asyncio
async def test_integration_real_catalog_scan_and_real_validation_succeed_together(conn, monkeypatch):
    """Unlike every other test in this file, this one does NOT monkeypatch
    refresh_institute_catalog or validate_and_sync_group_on_first_subscribe
    — only the actual network/file boundary (fetch_source_page_html,
    _load_pdf_bytes) is stubbed. This exercises the real sync-vs-async
    notify_admin contract between the two real functions, and would have
    caught the "immediate sync is a no-op" ordering bug on its own."""
    import schedule_bot.group_commands as group_commands_module
    import schedule_bot.group_links as group_links_module

    pdf_bytes = FIXTURE_PATH.read_bytes()
    catalog_html = '<a href="/ochnaya">Очная форма обучения</a>'

    def fetch(url):
        if url == "https://example.com/institut":
            return catalog_html
        if url == "https://example.com/ochnaya":
            return '<a href="/a.pdf">Б-Мв-101</a>'
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(group_links_module, "fetch_source_page_html", fetch)
    monkeypatch.setattr(group_commands_module, "_load_pdf_bytes", lambda source: pdf_bytes)

    config = _config()
    notify_admin = AsyncMock()
    sync_fn = AsyncMock(return_value=[])
    reschedule = MagicMock()

    # chat_id 999 backed up from a destructive schema rebuild (see
    # storage._backup_legacy_member_chat_ids) — no current `members` row
    # of its own, matching the real post-wipe state this migration
    # recovers from.
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS legacy_members_backup (chat_id INTEGER PRIMARY KEY);"
        "INSERT INTO legacy_members_backup (chat_id) VALUES (999);"
    )
    conn.commit()

    await run_legacy_migration_if_configured(conn, config, notify_admin, sync_fn, reschedule)

    institute = storage.get_institute_by_catalog_url(conn, "https://example.com/institut")
    assert institute is not None
    group = storage.get_group_by_name(conn, institute.id, "Б-Мв-101")
    assert group is not None
    assert group.is_verified == 1  # real find_header_column_index found the column in the real fixture PDF

    assert storage.get_member(conn, 999).group_id == group.id  # backed-up legacy user moved onto the real target group
    assert storage.get_setting(conn, "legacy_migration_done_at") is not None
    # sync_fn is called TWICE for real here: once by
    # validate_and_sync_group_on_first_subscribe itself (a no-op, since no
    # member is on the group yet at that point) and once by
    # legacy_migration's own explicit call after members are reassigned —
    # both with the same institute_link_id.
    assert sync_fn.await_count == 2
    for call in sync_fn.await_args_list:
        assert call.args == (group.institute_link_id,)
    reschedule.assert_called_once_with(institute.id)


@pytest.mark.asyncio
async def test_legacy_migration_only_migrates_backed_up_chat_ids_not_already_resubscribed(conn, monkeypatch):
    """A chat_id that re-subscribed via /start (its own fresh members row
    exists) after the legacy wipe must NOT be silently reassigned onto the
    legacy target group — only genuinely still-unmigrated legacy users
    should move."""
    import schedule_bot.legacy_migration as mod

    config = _config()
    conn.executescript(
        "CREATE TABLE IF NOT EXISTS legacy_members_backup (chat_id INTEGER PRIMARY KEY);"
        "INSERT INTO legacy_members_backup (chat_id) VALUES (111), (222), (333);"
    )
    conn.commit()

    def fake_refresh(conn_, institute, *, notify_admin):
        link_id = storage.create_institute_link(
            conn_, institute_id=institute.id, pdf_url="https://example.com/a.pdf",
            group_names=["Б-Мв-101"], raw_label="Б-Мв-101", last_seen_at="t",
        )
        storage.create_group(conn_, institute_id=institute.id, institute_link_id=link_id, name="Б-Мв-101")

    monkeypatch.setattr(mod, "refresh_institute_catalog", fake_refresh)
    monkeypatch.setattr(mod, "validate_and_sync_group_on_first_subscribe", AsyncMock(return_value=True))

    other_institute_id = storage.create_institute(
        conn, name="Другой", catalog_url="https://other.example.com",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )
    other_link_id = storage.create_institute_link(
        conn, institute_id=other_institute_id, pdf_url="https://other.example.com/x.pdf",
        group_names=["ДругаяГруппа"], raw_label="ДругаяГруппа", last_seen_at="t",
    )
    other_group_id = storage.create_group(conn, institute_id=other_institute_id, institute_link_id=other_link_id, name="ДругаяГруппа")
    storage.upsert_member(conn, 222, other_group_id, joined_at="t")

    notify_admin = AsyncMock()
    sync_institute_link_fn = AsyncMock()
    reschedule_institute_jobs = MagicMock()

    await run_legacy_migration_if_configured(conn, config, notify_admin, sync_institute_link_fn, reschedule_institute_jobs)

    institute = storage.get_institute_by_catalog_url(conn, "https://example.com/institut")
    target_group = storage.get_group_by_name(conn, institute.id, "Б-Мв-101")

    assert storage.get_member(conn, 111).group_id == target_group.id
    assert storage.get_member(conn, 333).group_id == target_group.id
    assert storage.get_member(conn, 222).group_id == other_group_id  # untouched

    # 222 was skipped (already resubscribed elsewhere), but it's just as
    # resolved as 111/333 — all three must be pruned from the backup, or
    # a later warn_if_legacy_backup_unconfigured would report it forever.
    assert storage.list_legacy_member_chat_ids_backup(conn) == []


@pytest.mark.asyncio
async def test_migration_preserves_original_subscribed_and_joined_at_and_prunes_backup(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    def fake_refresh(conn_, institute, *, notify_admin):
        link_id = storage.create_institute_link(
            conn_, institute_id=institute.id, pdf_url="https://example.com/a.pdf",
            group_names=["Б-Мв-101"], raw_label="Б-Мв-101", last_seen_at="t",
        )
        storage.create_group(conn_, institute_id=institute.id, institute_link_id=link_id, name="Б-Мв-101")

    monkeypatch.setattr(mod, "refresh_institute_catalog", fake_refresh)
    monkeypatch.setattr(mod, "validate_and_sync_group_on_first_subscribe", AsyncMock(return_value=True))
    config = _config()

    conn.executescript(
        "CREATE TABLE IF NOT EXISTS legacy_members_backup "
        "(chat_id INTEGER PRIMARY KEY, subscribed INTEGER NOT NULL DEFAULT 1, joined_at TEXT NOT NULL DEFAULT '');"
        "INSERT INTO legacy_members_backup (chat_id, subscribed, joined_at) "
        "VALUES (111, 0, '2025-01-01T00:00:00+00:00'), (222, 1, '2025-02-02T00:00:00+00:00');"
    )
    conn.commit()

    await run_legacy_migration_if_configured(
        conn, config, AsyncMock(), AsyncMock(), MagicMock(),
    )

    member_1 = storage.get_member(conn, 111)
    member_2 = storage.get_member(conn, 222)
    assert member_1.subscribed is False
    assert member_1.joined_at == "2025-01-01T00:00:00+00:00"
    assert member_2.subscribed is True
    assert member_2.joined_at == "2025-02-02T00:00:00+00:00"

    # Migrated chat_ids must be pruned from the backup table.
    assert storage.list_legacy_member_chat_ids_backup(conn) == []


@pytest.mark.asyncio
async def test_warn_if_legacy_backup_unconfigured_alerts_when_backup_nonempty():
    import sqlite3

    from schedule_bot.legacy_migration import warn_if_legacy_backup_unconfigured

    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    connection.executescript(
        "CREATE TABLE IF NOT EXISTS legacy_members_backup "
        "(chat_id INTEGER PRIMARY KEY, subscribed INTEGER NOT NULL DEFAULT 1, joined_at TEXT NOT NULL DEFAULT '');"
        "INSERT INTO legacy_members_backup (chat_id) VALUES (111);"
    )
    connection.commit()
    config = _config(legacy_migration_group_name=None)  # not configured
    notify_admin = AsyncMock()

    await warn_if_legacy_backup_unconfigured(connection, config, notify_admin)

    notify_admin.assert_awaited_once()
    assert "1" in notify_admin.call_args[0][0]


@pytest.mark.asyncio
async def test_warn_if_legacy_backup_unconfigured_is_silent_when_backup_empty(conn):
    from schedule_bot.legacy_migration import warn_if_legacy_backup_unconfigured

    config = _config(legacy_migration_group_name=None)  # not configured
    notify_admin = AsyncMock()

    await warn_if_legacy_backup_unconfigured(conn, config, notify_admin)

    notify_admin.assert_not_awaited()


@pytest.mark.asyncio
async def test_warn_if_legacy_backup_unconfigured_is_silent_when_configured(conn):
    from schedule_bot.legacy_migration import warn_if_legacy_backup_unconfigured

    conn.executescript(
        "CREATE TABLE IF NOT EXISTS legacy_members_backup "
        "(chat_id INTEGER PRIMARY KEY, subscribed INTEGER NOT NULL DEFAULT 1, joined_at TEXT NOT NULL DEFAULT '');"
        "INSERT INTO legacy_members_backup (chat_id) VALUES (111);"
    )
    conn.commit()
    config = _config()  # fully configured
    notify_admin = AsyncMock()

    await warn_if_legacy_backup_unconfigured(conn, config, notify_admin)

    notify_admin.assert_not_awaited()

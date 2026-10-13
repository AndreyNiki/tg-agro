import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.config import Config
from schedule_bot.legacy_migration import run_legacy_migration_if_configured


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

    # Two pre-existing members, each in a DIFFERENT old group, with
    # distinct subscribed/joined_at — migration must move BOTH, and must
    # not disturb those fields.
    other_institute_id = storage.create_institute(
        conn, name="Старый", catalog_url="https://old.example.com",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )
    old_link_id = storage.create_institute_link(
        conn, institute_id=other_institute_id, pdf_url="https://old.example.com/x.pdf",
        group_names=["Старая-1", "Старая-2"], raw_label="Старая-1,Старая-2", last_seen_at="t",
    )
    old_group_1 = storage.create_group(conn, institute_id=other_institute_id, institute_link_id=old_link_id, name="Старая-1")
    old_group_2 = storage.create_group(conn, institute_id=other_institute_id, institute_link_id=old_link_id, name="Старая-2")
    storage.upsert_member(conn, 111, old_group_1, joined_at="2025-01-01T00:00:00+00:00", subscribed=True)
    storage.upsert_member(conn, 222, old_group_2, joined_at="2025-02-02T00:00:00+00:00", subscribed=False)

    await run_legacy_migration_if_configured(conn, config, notify_admin, sync_fn, reschedule)

    institute = storage.get_institute_by_catalog_url(conn, "https://example.com/institut")
    target_group = storage.get_group_by_name(conn, institute.id, "Б-Мв-101")

    member_1 = storage.get_member(conn, 111)
    member_2 = storage.get_member(conn, 222)
    assert member_1.group_id == target_group.id
    assert member_1.subscribed is True
    assert member_1.joined_at == "2025-01-01T00:00:00+00:00"
    assert member_2.group_id == target_group.id
    assert member_2.subscribed is False
    assert member_2.joined_at == "2025-02-02T00:00:00+00:00"

    assert storage.get_setting(conn, "legacy_migration_done_at") is not None
    notify_admin.assert_awaited_once()

    # Second call must be a pure no-op now that the flag is set.
    notify_admin.reset_mock()
    monkeypatch.setattr(mod, "refresh_institute_catalog", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run again")))
    await run_legacy_migration_if_configured(conn, config, notify_admin, sync_fn, reschedule)
    notify_admin.assert_not_awaited()

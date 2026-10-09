import sqlite3
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.bot import day_callback
from schedule_bot.config import Config
from schedule_bot.weektype import WeekFormula
from main import build_application


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


@pytest.fixture(autouse=True)
def _shutdown_schedulers_after_test(monkeypatch):
    import main as main_module

    original_build_scheduler = main_module.build_scheduler
    schedulers = []

    def tracking_build_scheduler(*args, **kwargs):
        scheduler = original_build_scheduler(*args, **kwargs)
        schedulers.append(scheduler)
        return scheduler

    monkeypatch.setattr(main_module, "build_scheduler", tracking_build_scheduler)
    yield
    for scheduler in schedulers:
        if scheduler.running:
            scheduler.shutdown(wait=False)


@pytest.fixture
def config():
    return Config(
        telegram_token="test-token",
        admin_chat_id=999,
        db_path=":memory:",
        pdf_cache_dir="/tmp/cache",
        default_pdf_source="/tmp/does-not-matter.pdf",
        default_group_name="Б-Мв-101",
        default_weektype_url="https://example.com/week",
        default_daily_send_time="09:00",
        default_check_interval_minutes=60,
        default_timezone="Europe/Moscow",
        legacy_group_source_page_url=None,
    )


def _patch_week_formula(monkeypatch):
    monkeypatch.setattr(
        "main.fetch_week_formula", lambda url: WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    )


def test_build_application_registers_all_expected_handlers(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)

    command_names = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "commands"):
                command_names.update(handler.commands)

    assert {
        "start", "subscribe", "unsubscribe", "schedule", "help",
        "set_pdf_source", "set_send_time", "set_check_interval", "refresh", "alert",
    }.issubset(command_names)
    assert "stop" not in command_names
    assert "set_group" not in command_names


def test_build_application_seeds_only_weektype_url_default(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    build_application(config, conn)

    assert storage.get_setting(conn, "weektype_url") == "https://example.com/week"
    assert storage.get_setting(conn, "group_name") is None  # no longer seeded — would falsely trigger migration


def test_build_application_does_not_create_a_group_on_fresh_install(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    build_application(config, conn)

    assert storage.count_groups(conn) == 0


def test_build_application_migrates_legacy_single_group_database(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)
    storage.set_setting(conn, "group_name", "Б-Мв-101")
    storage.set_setting(conn, "pdf_source", "https://example.com/101.pdf")
    storage.set_setting(conn, "daily_send_time", "09:00")
    storage.set_setting(conn, "check_interval_minutes", "60")
    storage.set_setting(conn, "timezone", "Europe/Moscow")
    conn.execute(
        "INSERT INTO subscribers (chat_id, group_name, joined_at) VALUES (111, 'Б-Мв-101', '2026-09-16T00:00:00')"
    )
    conn.commit()
    config.legacy_group_source_page_url = "https://example.com/schedules"

    application = build_application(config, conn)

    assert storage.count_groups(conn) == 1
    group = storage.list_all_groups(conn)[0]
    assert group.name == "Б-Мв-101"
    assert storage.get_member(conn, 111).group_id == group.id
    application.bot_data["_scheduler"].shutdown(wait=False)


def test_build_application_week_formula_reflects_later_holder_updates(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)
    week_formula = application.bot_data["week_formula"]
    on_date = date(2026, 9, 16)
    first_result = week_formula.compute(on_date)

    holder = application.bot_data["_week_formula_holder"]
    holder.formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=False)

    assert week_formula.compute(on_date) != first_result


def test_build_application_persists_successfully_fetched_week_formula(conn, config, monkeypatch):
    from schedule_bot.weektype import load_week_formula

    fetched = WeekFormula(base_month=3, base_day=17, even_index_is_upper=False)
    monkeypatch.setattr("main.fetch_week_formula", lambda url: fetched)

    build_application(config, conn)

    assert load_week_formula(conn) == fetched


def test_build_application_falls_back_to_persisted_formula_on_fetch_failure(conn, config, monkeypatch):
    from schedule_bot.weektype import save_week_formula

    persisted_formula = WeekFormula(base_month=2, base_day=1, even_index_is_upper=False)
    save_week_formula(conn, persisted_formula)

    def raise_error(url):
        raise ValueError("Week type formula pattern not found in page HTML")

    monkeypatch.setattr("main.fetch_week_formula", raise_error)

    application = build_application(config, conn)

    assert application.bot_data["_week_formula_holder"].formula == persisted_formula


def test_build_application_falls_back_to_hardcoded_default_when_nothing_persisted(conn, config, monkeypatch):
    import main as main_module

    def raise_error(url):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr("main.fetch_week_formula", raise_error)

    application = build_application(config, conn)

    assert application.bot_data["_week_formula_holder"].formula == main_module._FALLBACK_WEEK_FORMULA


@pytest.mark.asyncio
async def test_day_callback_through_real_bot_data_replies_gracefully_without_active_version(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")

    update = MagicMock()
    update.effective_chat.id = 111
    update.callback_query.data = f"day:{date(2026, 9, 16).isoformat()}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()

    context = MagicMock()
    context.bot_data = application.bot_data

    await day_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_not_awaited()

    application.bot_data["_scheduler"].shutdown(wait=False)


@pytest.mark.asyncio
async def test_day_callback_through_real_bot_data_replies_with_schedule_when_active_version_exists(conn, config, monkeypatch, tmp_path):
    from pathlib import Path

    _patch_week_formula(monkeypatch)

    fixture_pdf = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")
    config.pdf_cache_dir = str(tmp_path)

    application = build_application(config, conn)
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url=fixture_pdf,
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")

    sync_fn = application.bot_data["sync_fn"]
    result = await sync_fn(group_id)
    assert result.status == "initial"

    update = MagicMock()
    update.effective_chat.id = 111
    update.callback_query.data = f"day:{date(2026, 9, 16).isoformat()}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()

    context = MagicMock()
    context.bot_data = application.bot_data

    await day_callback(update, context)

    reply_text = update.callback_query.message.reply_text.call_args[0][0]
    assert "ещё не загружено" not in reply_text

    application.bot_data["_scheduler"].shutdown(wait=False)


@pytest.mark.asyncio
async def test_post_init_backfills_missing_member_profiles(conn, config, monkeypatch):
    """A member migrated from the legacy subscribers table (or created
    through some path that didn't capture a profile) must get its
    username/display_name backfilled via bot.get_chat during startup."""
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")

    # application.bot is a real (frozen) ExtBot instance — TelegramObject
    # forbids setting instance attributes like `get_chat`/`set_my_commands`
    # directly once frozen, so patch the methods at the class level instead
    # (monkeypatch auto-reverts after the test). set_my_commands must also
    # be stubbed here since post_init calls it before the backfill, and a
    # real call would hit the live Telegram API with the fake test token.
    fake_chat = MagicMock(username="ivan", full_name="Ivan Ivanov")
    monkeypatch.setattr(type(application.bot), "get_chat", AsyncMock(return_value=fake_chat))
    monkeypatch.setattr(type(application.bot), "set_my_commands", AsyncMock(return_value=True))

    post_init = application.post_init
    await post_init(application)

    member = storage.get_member(conn, 111)
    assert member.telegram_username == "ivan"
    assert member.display_name == "Ivan Ivanov"

    application.bot_data["_scheduler"].shutdown(wait=False)


@pytest.mark.asyncio
async def test_post_init_backfill_isolates_per_chat_failures(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")

    async def get_chat(self, chat_id, *args, **kwargs):
        if chat_id == 111:
            raise RuntimeError("bot blocked by user")
        return MagicMock(username="ivan", full_name="Ivan Ivanov")

    monkeypatch.setattr(type(application.bot), "get_chat", get_chat)
    monkeypatch.setattr(type(application.bot), "set_my_commands", AsyncMock(return_value=True))

    await application.post_init(application)  # must not raise

    assert storage.get_member(conn, 222).telegram_username == "ivan"
    assert storage.get_member(conn, 111).telegram_username is None

    application.bot_data["_scheduler"].shutdown(wait=False)

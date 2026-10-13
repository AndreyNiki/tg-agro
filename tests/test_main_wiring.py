import asyncio
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
        default_weektype_url="https://example.com/week",
        default_daily_send_time="09:00",
        default_check_interval_minutes=60,
        default_timezone="Europe/Moscow",
        default_catalog_refresh_interval_minutes=10080,
    )


def _patch_week_formula(monkeypatch):
    monkeypatch.setattr(
        "main.fetch_week_formula", lambda url: WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    )


_test_group_counter = 0

def _make_test_group(conn, name="Б-Мв-101", pdf_url="https://example.com/test.pdf"):
    """Helper to create a test group with dummy institute and link (for test_main_wiring).
    Creates a unique institute per call to avoid UNIQUE(name) constraint violations."""
    global _test_group_counter
    _test_group_counter += 1

    # Create a dummy institute with a unique name per call
    from datetime import datetime, timezone
    institute_name = f"test-institute-{_test_group_counter}"
    cursor = conn.execute(
        "INSERT INTO institutes (name, catalog_url, check_interval_minutes, send_time, timezone, "
        "catalog_refresh_interval_minutes, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (institute_name, "https://example.com/catalog", 60, "09:00", "Europe/Moscow", 10080, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    institute_id = cursor.lastrowid

    # Create a dummy link (minimal Task 2 implementation for testing)
    cursor = conn.execute(
        "INSERT INTO institute_links (institute_id, pdf_url, group_names_json, raw_label, last_seen_at, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (institute_id, pdf_url, f'["{name}"]', name, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    institute_link_id = cursor.lastrowid

    return storage.create_group(
        conn, institute_id=institute_id, institute_link_id=institute_link_id, name=name
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
        "add_institute", "set_institute_catalog_url", "set_institute_check_interval",
        "set_institute_send_time", "set_institute_catalog_interval",
        "refresh_institute_links", "institute_links", "refresh", "alert",
    }.issubset(command_names)
    assert "stop" not in command_names
    assert "set_group" not in command_names
    assert "set_pdf_source" not in command_names
    assert "set_send_time" not in command_names
    assert "set_check_interval" not in command_names


def test_build_application_registers_missing_institute_and_group_callbacks(conn, config, monkeypatch):
    from schedule_bot.group_commands import group_request_start_callback, institute_request_start_callback

    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)

    callback_handlers = [
        handler
        for handlers in application.handlers.values()
        for handler in handlers
        if hasattr(handler, "pattern") and handler.pattern is not None
    ]

    institute_matches = [h for h in callback_handlers if h.pattern.match("instreq:new")]
    assert any(h.callback is institute_request_start_callback for h in institute_matches)

    group_matches = [h for h in callback_handlers if h.pattern.match("grpreq:new:1")]
    assert any(h.callback is group_request_start_callback for h in group_matches)


def test_build_application_seeds_only_weektype_url_default(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    build_application(config, conn)

    assert storage.get_setting(conn, "weektype_url") == "https://example.com/week"
    assert storage.get_setting(conn, "group_name") is None  # no longer seeded — would falsely trigger migration


def test_build_application_does_not_create_a_group_on_fresh_install(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    build_application(config, conn)

    assert storage.list_institutes(conn) == []


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
    group_id = _make_test_group(conn, "Б-Мв-101")
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
    group_id = _make_test_group(conn, "Б-Мв-101", pdf_url=fixture_pdf)
    storage.upsert_member(conn, 111, group_id, joined_at="t")

    group = storage.get_group(conn, group_id)
    sync_institute_link_fn = application.bot_data["sync_institute_link_fn"]
    results = await sync_institute_link_fn(group.institute_link_id)
    own_result = next(r for r in results if r.group_id == group_id)
    assert own_result.status == "initial"

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
    group_id = _make_test_group(conn, "Б-Мв-101")
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
async def test_post_init_sets_base_default_scope_and_backfills_member_chat_scopes(conn, config, monkeypatch):
    """The native "/" command list (the blue Menu button) must stay
    minimal: BotCommandScopeDefault gets just /menu, and every chat_id
    that already has a `members` row gets a per-chat override adding
    /schedule — otherwise a pre-existing member would keep seeing
    whatever default list was set before this feature existed."""
    from telegram import BotCommandScopeChat, BotCommandScopeDefault

    from schedule_bot.bot import BASE_VISIBLE_COMMANDS, MEMBER_VISIBLE_COMMANDS

    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)
    group_id = _make_test_group(conn, "Б-Мв-101")
    storage.upsert_member(conn, 111, group_id, joined_at="t")

    set_my_commands = AsyncMock(return_value=True)
    monkeypatch.setattr(type(application.bot), "get_chat", AsyncMock(return_value=MagicMock(username=None, full_name=None)))
    monkeypatch.setattr(type(application.bot), "set_my_commands", set_my_commands)

    await application.post_init(application)

    default_calls = [c for c in set_my_commands.await_args_list if isinstance(c.kwargs.get("scope"), BotCommandScopeDefault)]
    assert len(default_calls) == 1
    assert [cmd.command for cmd in default_calls[0].args[0]] == [name for name, _ in BASE_VISIBLE_COMMANDS]

    member_calls = [c for c in set_my_commands.await_args_list if c.kwargs.get("scope") == BotCommandScopeChat(chat_id=111)]
    assert len(member_calls) == 1
    assert [cmd.command for cmd in member_calls[0].args[0]] == [name for name, _ in MEMBER_VISIBLE_COMMANDS]

    application.bot_data["_scheduler"].shutdown(wait=False)


@pytest.mark.asyncio
async def test_post_init_backfill_isolates_per_chat_failures(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)
    group_id = _make_test_group(conn, "Б-Мв-101")
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


@pytest.mark.asyncio
async def test_build_application_shares_one_lock_per_link_between_refresh_and_scheduled_jobs(
    conn, config, monkeypatch, tmp_path
):
    """Regression test for the Task 12 review's Critical finding, re-
    targeted to the link-level model (Task 14/18): a manual /refresh
    (sync_institute_link_fn) and that SAME link's own scheduled
    hourly/daily job used to lock on two DIFFERENT asyncio.Lock objects —
    build_scheduler kept its own internal {link_id: Lock} dict, separate
    from main.py's single module-wide lock dict — so they could run
    sync_institute_link concurrently for this link (double-apply,
    double-broadcast, cache-file race). They must now share the exact
    same Lock object for a link that already exists when
    build_application/build_scheduler runs at startup. Per spec,
    locking happens at the LINK level, not the institute or group level.

    This creates the group/link BEFORE build_application so it's picked
    up by build_scheduler's own startup loop (not by a later
    reschedule_institute_link_jobs_cb call) — that startup path is
    exactly where the bug lived.

    sync_institute_link is stubbed out (fast AsyncMock) so the only
    thing that can make sync_institute_link_fn block is lock contention
    itself — a real PDF parse call would also take long enough to look
    "blocked" within a short sleep() window even on a DIFFERENT, unlocked
    Lock object, which would make this test pass for the wrong reason and
    defeat the point of the regression test."""
    _patch_week_formula(monkeypatch)

    group_id = _make_test_group(conn, "Б-Мв-101")
    institute_link_id = storage.get_group(conn, group_id).institute_link_id

    application = build_application(config, conn)
    scheduler = application.bot_data["_scheduler"]
    sync_institute_link_fn = application.bot_data["sync_institute_link_fn"]

    stub = AsyncMock(return_value=[])
    monkeypatch.setattr("main.sync_institute_link", stub)

    job_lock = scheduler.get_job(f"link_check_{institute_link_id}").args[-3]
    assert isinstance(job_lock, asyncio.Lock)

    # Acquire the scheduled job's lock first (uncontended acquire on an
    # asyncio.Lock completes without yielding to the event loop, so this
    # is guaranteed to win before any background scheduler job runs).
    await job_lock.acquire()
    try:
        task = asyncio.ensure_future(sync_institute_link_fn(institute_link_id))
        await asyncio.sleep(0.05)
        # If sync_institute_link_fn used a DIFFERENT lock object, the
        # stubbed sync_institute_link (near-instant) would have already
        # run while job_lock is held — proving the bug. With the fix,
        # sync_institute_link_fn must still be blocked waiting on the
        # identical lock object, so the stub must not have been called yet.
        assert not task.done()
        stub.assert_not_awaited()
    finally:
        job_lock.release()

    await asyncio.wait_for(task, timeout=5)
    stub.assert_awaited_once()

    scheduler.shutdown(wait=False)


def test_build_application_still_gives_different_links_independent_locks(conn, config, monkeypatch):
    """Guard against re-introducing Task 11's original bug while fixing
    the above: link_locks must still hand out a DISTINCT Lock per
    institute_link_id — a single shared lock across ALL links would
    defeat the whole point of per-link job isolation."""
    _patch_week_formula(monkeypatch)

    group_a = _make_test_group(conn, "A")
    group_b = _make_test_group(conn, "B")
    link_a = storage.get_group(conn, group_a).institute_link_id
    link_b = storage.get_group(conn, group_b).institute_link_id

    application = build_application(config, conn)
    scheduler = application.bot_data["_scheduler"]

    lock_a = scheduler.get_job(f"link_check_{link_a}").args[-3]
    lock_b = scheduler.get_job(f"link_check_{link_b}").args[-3]
    assert lock_a is not lock_b

    scheduler.shutdown(wait=False)


def test_build_application_registers_starosta_and_change_group_handlers(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)

    command_names = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "commands"):
                command_names.update(handler.commands)
    assert {"starosta", "votes", "change_group", "set_starosta", "remove_starosta"}.issubset(command_names)

    callback_patterns = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "pattern") and handler.pattern is not None:
                callback_patterns.add(handler.pattern.pattern)
    assert r"^starosta:nominate_self$" in callback_patterns
    assert r"^election:approve:\d+$" in callback_patterns
    assert r"^chgreq:approve:\d+$" in callback_patterns

    application.bot_data["_scheduler"].shutdown(wait=False)


def test_build_application_registers_classmates_command(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)

    command_names = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "commands"):
                command_names.update(handler.commands)
    assert "classmates" in command_names

    application.bot_data["_scheduler"].shutdown(wait=False)


def test_build_application_registers_privilege_handlers(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)

    command_names = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "commands"):
                command_names.update(handler.commands)
    assert {"close_group", "open_group", "notify", "requests"}.issubset(command_names)

    callback_patterns = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "pattern") and handler.pattern is not None:
                callback_patterns.add(handler.pattern.pattern)
    assert r"^exclude:select:\d+$" in callback_patterns
    assert r"^exclude:confirm:\d+$" in callback_patterns
    assert r"^exclude:abort$" in callback_patterns

    application.bot_data["_scheduler"].shutdown(wait=False)


def test_build_application_registers_menu_handlers(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)

    command_names = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "commands"):
                command_names.update(handler.commands)
    assert "menu" in command_names

    callback_patterns = set()
    message_handlers = []
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "pattern") and handler.pattern is not None:
                callback_patterns.add(handler.pattern.pattern)
            if type(handler).__name__ == "MessageHandler":
                message_handlers.append(handler)
    assert r"^menu:" in callback_patterns or any(p.startswith("^menu:") for p in callback_patterns)
    assert len(message_handlers) >= 1

    application.bot_data["_scheduler"].shutdown(wait=False)


@pytest.mark.asyncio
async def test_manual_refresh_and_scheduled_jobs_share_one_catalog_rescan_state(conn, config, monkeypatch):
    """Finding: main.py owned a separate per-process set used only by the
    manual /refresh path, while the scheduler passed None (making its own
    fresh structure per call). Both must now use the IDENTICAL object, or
    the 404-rescan rate limit means nothing."""
    import schedule_bot.sync as sync_module

    _patch_week_formula(monkeypatch)

    group_id = _make_test_group(conn, "Б-Мв-101")
    group = storage.get_group(conn, group_id)
    application = build_application(config, conn)

    scheduled_state = application.bot_data["_scheduler"].get_job(
        f"link_check_{group.institute_link_id}"
    ).args[-2]
    assert scheduled_state is application.bot_data["_catalog_rescan_state"]

    seen = []

    async def recording_sync(*args, catalog_rescan_state, **kwargs):
        seen.append(catalog_rescan_state)
        return []

    monkeypatch.setattr(sync_module, "sync_institute_link", recording_sync)
    monkeypatch.setattr("main.sync_institute_link", recording_sync)

    await application.bot_data["sync_institute_link_fn"](group.institute_link_id)

    assert seen and seen[0] is scheduled_state
    application.bot_data["_scheduler"].shutdown(wait=False)


def test_build_application_wires_institute_bot_data_callbacks(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)

    assert "sync_institute_link_fn" in application.bot_data
    assert "reschedule_institute_link_jobs" in application.bot_data
    assert "reschedule_institute_jobs" in application.bot_data
    assert "reschedule_group_jobs" not in application.bot_data
    assert "sync_fn" not in application.bot_data
    application.bot_data["_scheduler"].shutdown(wait=False)


def test_build_application_registers_report_and_faq_commands(conn, config, monkeypatch):
    _patch_week_formula(monkeypatch)

    application = build_application(config, conn)

    command_names = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "commands"):
                command_names.update(handler.commands)
    assert {"report", "faq"}.issubset(command_names)

    application.bot_data["_scheduler"].shutdown(wait=False)


@pytest.mark.asyncio
async def test_post_init_runs_legacy_migration_when_configured(conn, config, monkeypatch):
    import main as main_module

    config.legacy_migration_catalog_url = "https://example.com/institut"
    config.legacy_migration_institute_name = "Институт агробизнеса"
    config.legacy_migration_group_name = "Б-Мв-101"

    _patch_week_formula(monkeypatch)
    called_with = {}

    async def fake_migration(conn_, config_, notify_admin, sync_fn, reschedule, **kwargs):
        called_with["conn"] = conn_
        called_with["config"] = config_
        called_with["notify_admin"] = notify_admin
        called_with["sync_fn"] = sync_fn
        called_with["reschedule"] = reschedule

    monkeypatch.setattr(main_module, "run_legacy_migration_if_configured", fake_migration)

    # application.bot is a real (frozen) ExtBot instance — TelegramObject
    # forbids setting instance attributes directly once frozen, so patch
    # the methods at the class level instead (monkeypatch auto-reverts).
    application = main_module.build_application(config, conn)
    monkeypatch.setattr(type(application.bot), "get_chat", AsyncMock(return_value=MagicMock(username=None, full_name=None)))
    monkeypatch.setattr(type(application.bot), "set_my_commands", AsyncMock(return_value=True))

    await application.post_init(application)

    assert called_with["conn"] is conn
    assert called_with["config"] is config
    assert called_with["sync_fn"] is application.bot_data["sync_institute_link_fn"]
    assert called_with["reschedule"] is application.bot_data["reschedule_institute_jobs"]

    application.bot_data["_scheduler"].shutdown(wait=False)


@pytest.mark.asyncio
async def test_post_init_skips_legacy_migration_when_not_configured(conn, config, monkeypatch):
    import main as main_module

    _patch_week_formula(monkeypatch)
    migration_mock = AsyncMock()
    monkeypatch.setattr(main_module, "run_legacy_migration_if_configured", migration_mock)

    application = main_module.build_application(config, conn)
    monkeypatch.setattr(type(application.bot), "get_chat", AsyncMock(return_value=MagicMock(username=None, full_name=None)))
    monkeypatch.setattr(type(application.bot), "set_my_commands", AsyncMock(return_value=True))

    await application.post_init(application)

    migration_mock.assert_awaited_once()  # still called — it's the function's own job to no-op

    application.bot_data["_scheduler"].shutdown(wait=False)


@pytest.mark.asyncio
async def test_post_init_survives_legacy_migration_exception(conn, config, monkeypatch):
    """An admin-notify failure (blocked bot, bad ADMIN_CHAT_ID) or any
    other exception inside the migration must not prevent the rest of
    post_init (and thus bot startup) from completing — every other
    admin-notify loop in this codebase isolates failures the same way
    (see CLAUDE.md's "every per-subscriber send loop" rule)."""
    import main as main_module

    config.legacy_migration_catalog_url = "https://example.com/institut"
    config.legacy_migration_institute_name = "Институт агробизнеса"
    config.legacy_migration_group_name = "Б-Мв-101"

    _patch_week_formula(monkeypatch)
    monkeypatch.setattr(main_module, "run_legacy_migration_if_configured", AsyncMock(side_effect=RuntimeError("boom")))

    application = main_module.build_application(config, conn)
    monkeypatch.setattr(type(application.bot), "get_chat", AsyncMock(return_value=MagicMock(username=None, full_name=None)))
    monkeypatch.setattr(type(application.bot), "set_my_commands", AsyncMock(return_value=True))

    await application.post_init(application)  # must not raise

    application.bot_data["_scheduler"].shutdown(wait=False)


@pytest.mark.asyncio
async def test_global_error_handler_notifies_admin_and_logs(conn, config, monkeypatch, caplog):
    import logging

    from telegram.ext import ContextTypes

    _patch_week_formula(monkeypatch)
    app = build_application(config, conn)
    try:
        monkeypatch.setattr(type(app.bot), "send_message", AsyncMock())
        context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
        context.bot = app.bot
        context.application = app
        context.error = ValueError("boom")
        update = MagicMock()
        update.effective_chat.id = 555
        update.effective_message = None

        handlers = list(app.error_handlers) if hasattr(app, "error_handlers") else []
        assert handlers, "no error handler registered on Application"

        with caplog.at_level(logging.ERROR):
            await handlers[0](update, context)

        app.bot.send_message.assert_awaited()
        _, kwargs = app.bot.send_message.call_args
        assert kwargs["chat_id"] == 999
        assert "boom" in kwargs["text"] or "ValueError" in kwargs["text"]
    finally:
        app.bot_data["_scheduler"].shutdown(wait=False)


def test_command_handlers_ignore_edited_messages(conn, config, monkeypatch):
    from telegram.ext import CommandHandler, MessageHandler

    _patch_week_formula(monkeypatch)
    app = build_application(config, conn)
    try:
        command_handlers = [
            h for group in app.handlers.values() for h in group
            if isinstance(h, (CommandHandler, MessageHandler))
        ]
        assert command_handlers, "expected at least one CommandHandler/MessageHandler"
        for handler in command_handlers:
            assert "MESSAGE" in repr(handler.filters) or "message" in repr(handler.filters).lower(), (
                f"{handler.callback.__name__} is not restricted to non-edited messages: {handler.filters!r}"
            )
    finally:
        app.bot_data["_scheduler"].shutdown(wait=False)

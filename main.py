import asyncio
import logging
import os

from telegram import BotCommand, BotCommandScopeChat
from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from schedule_bot import storage
from schedule_bot.bot import (
    ADMIN_COMMANDS,
    NOOP_CALLBACK_DATA,
    USER_COMMANDS,
    alert_command,
    day_callback,
    help_command,
    noop_callback,
    refresh_command,
    schedule_command,
    set_check_interval_command,
    set_pdf_source_command,
    set_send_time_command,
)
from schedule_bot.config import Config, load_config
from schedule_bot.group_commands import (
    PAGE_CALLBACK_PATTERN,
    SELECT_CALLBACK_PATTERN,
    group_creation_handler,
    group_page_callback,
    group_select_callback,
    start_command,
    subscribe_command,
    unsubscribe_command,
)
from schedule_bot.scheduler import (
    WeekFormulaHolder,
    build_scheduler,
    reschedule_group_jobs,
)
from schedule_bot.sync import check_and_apply_diff
from schedule_bot.telegram_utils import send_with_retry
from schedule_bot.weektype import WeekFormula, fetch_week_formula, load_week_formula, save_week_formula

logger = logging.getLogger(__name__)

_FALLBACK_WEEK_FORMULA = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)


class _LiveWeekFormula:
    def __init__(self, holder: WeekFormulaHolder) -> None:
        self._holder = holder

    def compute(self, on_date):
        return self._holder.formula.compute(on_date)


async def _backfill_member_profiles(conn, bot) -> None:
    for chat_id in storage.list_members_missing_profile(conn):
        try:
            chat = await bot.get_chat(chat_id)
        except Exception as error:
            logger.warning("main: failed to backfill profile for chat_id=%s: %s", chat_id, error)
            continue
        storage.update_member_profile(conn, chat_id, telegram_username=chat.username, display_name=chat.full_name)


def build_application(config: Config, conn) -> Application:
    # Only weektype_url stays a bot-wide setting (Global Constraints) —
    # group_name/pdf_source/daily_send_time/check_interval_minutes/timezone
    # must NOT be re-seeded here: doing so would make
    # migrate_legacy_single_group think a fresh install has legacy data
    # to migrate, auto-creating an unwanted group every time.
    storage.ensure_default_settings(conn, {"weektype_url": config.default_weektype_url})
    storage.migrate_legacy_single_group(conn, config.legacy_group_source_page_url)

    week_formula = _fetch_week_formula_or_fallback(conn, storage.get_setting(conn, "weektype_url"))
    week_formula_holder = WeekFormulaHolder(formula=week_formula)

    async def _post_init(app: Application) -> None:
        await app.bot.set_my_commands([BotCommand(name, description) for name, description in USER_COMMANDS])
        await app.bot.set_my_commands(
            [BotCommand(name, description) for name, description in USER_COMMANDS + ADMIN_COMMANDS],
            scope=BotCommandScopeChat(chat_id=config.admin_chat_id),
        )
        await _backfill_member_profiles(conn, app.bot)

    application = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(_post_init)
        .concurrent_updates(True)
        .build()
    )
    application.bot_data["conn"] = conn
    application.bot_data["config"] = config
    application.bot_data["admin_chat_id"] = config.admin_chat_id
    application.bot_data["_week_formula_holder"] = week_formula_holder
    application.bot_data["week_formula"] = _LiveWeekFormula(week_formula_holder)
    application.bot_data["active_schedule_getter"] = lambda group_id: _load_active_schedule(conn, group_id)
    application.bot_data["active_pdf_path_getter"] = lambda group_id: _load_active_pdf_path(conn, group_id)

    sync_lock = asyncio.Lock()

    async def sync_fn(group_id: int):
        group = storage.get_group(conn, group_id)
        if group is None:
            raise ValueError(f"Group {group_id} not found")

        async def notify(chat_id, text):
            await send_with_retry(lambda: application.bot.send_message(chat_id=chat_id, text=text))

        async with sync_lock:
            return await check_and_apply_diff(conn, group, config.pdf_cache_dir, notify)

    application.bot_data["sync_fn"] = sync_fn

    scheduler_holder: dict = {}

    def reschedule_group_jobs_cb(group_id: int) -> None:
        reschedule_group_jobs(
            scheduler_holder["scheduler"], conn, config, application.bot, group_id, week_formula_holder, sync_lock
        )

    application.bot_data["reschedule_group_jobs"] = reschedule_group_jobs_cb

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(day_callback, pattern=r"^day:\d{4}-\d{2}-\d{2}$"))
    application.add_handler(CallbackQueryHandler(noop_callback, pattern=f"^{NOOP_CALLBACK_DATA}$"))
    application.add_handler(CallbackQueryHandler(group_select_callback, pattern=SELECT_CALLBACK_PATTERN))
    application.add_handler(CallbackQueryHandler(group_page_callback, pattern=PAGE_CALLBACK_PATTERN))
    application.add_handler(group_creation_handler)
    application.add_handler(CommandHandler("set_pdf_source", set_pdf_source_command))
    application.add_handler(CommandHandler("set_send_time", set_send_time_command))
    application.add_handler(CommandHandler("set_check_interval", set_check_interval_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("alert", alert_command))

    # NOTE: build_scheduler's `lock` parameter is dead for this call site —
    # as of Task 11's scheduler.py rewrite, build_scheduler creates its own
    # lazily-created per-group lock dict internally and never reads the
    # `lock` argument at all (see schedule_bot/scheduler.py's docstring on
    # build_scheduler). Passing sync_lock here would do nothing but imply
    # a connection between this module-wide lock and per-group scheduled
    # jobs that doesn't exist — so it's deliberately omitted.
    scheduler_holder["scheduler"] = build_scheduler(conn, config, application.bot, week_formula_holder)
    application.bot_data["_scheduler"] = scheduler_holder["scheduler"]

    return application


def _load_active_schedule(conn, group_id: int):
    import json

    from schedule_bot.pdf_parser import schedule_from_dict

    active = storage.get_active_version(conn, group_id)
    if active is None:
        return None
    return schedule_from_dict(json.loads(active.payload_json))


def _fetch_week_formula_or_fallback(conn, url):
    try:
        formula = fetch_week_formula(url)
        save_week_formula(conn, formula)
        return formula
    except Exception as error:
        persisted = load_week_formula(conn)
        if persisted is not None:
            logger.warning(
                "main: failed to fetch week formula from %r (%s); falling back to last persisted formula", url, error
            )
            return persisted
        logger.warning(
            "main: failed to fetch week formula from %r (%s); no persisted formula available, "
            "falling back to hardcoded default",
            url, error,
        )
        return _FALLBACK_WEEK_FORMULA


def _load_active_pdf_path(conn, group_id: int):
    active = storage.get_active_version(conn, group_id)
    if active is None:
        return None
    return active.pdf_cache_path


if __name__ == "__main__":
    import sqlite3

    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    config = load_config(os.environ)
    connection = sqlite3.connect(config.db_path, check_same_thread=False)
    connection.execute("PRAGMA journal_mode=WAL")
    storage.init_db(connection)
    app = build_application(config, connection)
    app.run_polling()

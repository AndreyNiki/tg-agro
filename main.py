import asyncio
import logging
import os

from telegram import BotCommand, BotCommandScopeDefault
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from schedule_bot import storage
from schedule_bot.menu import menu_callback, menu_command, pending_action_followup
from schedule_bot.bot import (
    BASE_VISIBLE_COMMANDS,
    NOOP_CALLBACK_DATA,
    alert_command,
    close_group_command,
    day_callback,
    help_command,
    noop_callback,
    notify_command,
    open_group_command,
    refresh_command,
    schedule_command,
    remove_starosta_command,
    set_starosta_command,
    sync_visible_commands,
)
from schedule_bot.classmates import classmates_command, exclude_abort_callback, exclude_confirm_callback, exclude_select_callback
from schedule_bot.faq import faq_command
from schedule_bot.report import report_command
from schedule_bot.change_group_commands import (
    change_group_abort_callback,
    change_group_command,
    change_group_confirm_callback,
    change_group_page_callback,
    change_group_select_callback,
    change_request_approve_callback,
    change_request_reject_callback,
    requests_command,
)
from schedule_bot.config import Config, load_config
from schedule_bot.group_commands import (
    INSTITUTE_PAGE_CALLBACK_PATTERN,
    INSTITUTE_SELECT_CALLBACK_PATTERN,
    PAGE_CALLBACK_PATTERN,
    SELECT_CALLBACK_PATTERN,
    group_page_callback,
    group_select_callback,
    institute_page_callback,
    institute_select_callback,
    start_command,
    subscribe_command,
    unsubscribe_command,
)
from schedule_bot.institute_commands import (
    add_institute_command,
    institute_links_command,
    refresh_institute_links_command,
    set_institute_catalog_interval_command,
    set_institute_catalog_url_command,
    set_institute_check_interval_command,
    set_institute_send_time_command,
)
from schedule_bot.scheduler import (
    WeekFormulaHolder,
    build_scheduler,
    reschedule_institute_jobs,
    reschedule_institute_link_jobs,
)
from schedule_bot.starosta_candidates import (
    abort_callback as starcand_abort_callback,
    confirm_callback as starcand_confirm_callback,
    page_callback as starcand_page_callback,
    select_callback as starcand_select_callback,
)
from schedule_bot.starosta_commands import (
    election_approve_callback,
    election_reject_callback,
    nominate_self_callback,
    starosta_change_callback,
    starosta_command,
    votes_command,
)
from schedule_bot.legacy_migration import run_legacy_migration_if_configured
from schedule_bot.sync import sync_institute_link
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
    storage.ensure_default_settings(conn, {"weektype_url": config.default_weektype_url})

    week_formula = _fetch_week_formula_or_fallback(conn, storage.get_setting(conn, "weektype_url"))
    week_formula_holder = WeekFormulaHolder(formula=week_formula)

    async def _post_init(app: Application) -> None:
        # Default scope covers chats with no membership yet (never
        # interacted, or excluded) — just /menu. Existing members get a
        # per-chat override below adding /schedule; sync_visible_commands
        # is also called at every point membership is gained/lost so this
        # backfill loop only matters for chats whose override predates
        # this change or was never set (bot restarted without any
        # membership change in between).
        await app.bot.set_my_commands(
            [BotCommand(name, description) for name, description in BASE_VISIBLE_COMMANDS],
            scope=BotCommandScopeDefault(),
        )
        # admin_chat_id used to get its own BotCommandScopeChat override
        # with the FULL command list (USER_COMMANDS + ADMIN_COMMANDS).
        # Telegram persists a chat-scope override server-side until it is
        # explicitly overwritten, so that old override must be reset here
        # too, even if the admin chat has no `members` row of its own.
        chat_ids = set(storage.list_all_member_chat_ids(conn))
        chat_ids.add(config.admin_chat_id)
        for chat_id in chat_ids:
            try:
                await sync_visible_commands(app.bot, conn, chat_id)
            except Exception as error:
                logger.warning("main: failed to sync visible commands for chat_id=%s: %s", chat_id, error)
        await _backfill_member_profiles(conn, app.bot)

        async def _notify_admin(text: str) -> None:
            await send_with_retry(lambda: app.bot.send_message(chat_id=config.admin_chat_id, text=text))

        try:
            await run_legacy_migration_if_configured(
                conn, config, _notify_admin, sync_institute_link_fn, reschedule_institute_jobs_cb,
            )
        except Exception:
            logger.exception("main: legacy user migration failed — bot startup continues regardless")

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

    # One Lock per institute_link_id — shared by the scheduler's own
    # link_check_job/link_daily_send_job (via the SAME dict passed into
    # build_scheduler) AND manual /refresh (sync_institute_link_fn,
    # below). This is what guarantees a manual /refresh for a group and
    # that group's link's own scheduled sync never run sync_institute_link
    # concurrently for the SAME link — mirrors the pre-institutes
    # group_locks pattern, just re-keyed to institute_link_id (the new
    # unit of sync work; see scheduler.py). Spec: "Планировщик заводит
    # job'ы на уровне ссылки, не института" — locking stays at the link
    # level, never institute- or group-level.
    link_locks: dict[int, asyncio.Lock] = {}
    # institute_id -> time.monotonic() of that institute's last 404-triggered
    # catalog rescan. ONE dict for the whole process: it is passed into
    # build_scheduler (so every link_check_job/link_daily_send_job of every
    # link shares it) AND used by the manual /refresh path below, so the
    # "one catalog rescan per institute per check interval" limit holds
    # across all of them. A timestamp (not a per-process "already done"
    # set) is what makes the limit self-resetting — see
    # sync._catalog_rescan_allowed.
    catalog_rescan_state: dict[int, float] = {}

    async def sync_institute_link_fn(institute_link_id: int):
        institute_link = storage.get_institute_link(conn, institute_link_id)
        if institute_link is None:
            raise ValueError(f"Institute link {institute_link_id} not found")
        institute = storage.get_institute(conn, institute_link.institute_id)

        async def notify(chat_id, text):
            await send_with_retry(lambda: application.bot.send_message(chat_id=chat_id, text=text))

        async def notify_admin(text):
            await send_with_retry(
                lambda: application.bot.send_message(chat_id=config.admin_chat_id, text=text)
            )

        lock = link_locks.setdefault(institute_link_id, asyncio.Lock())
        async with lock:
            return await sync_institute_link(
                conn, institute_link, institute, config.pdf_cache_dir, notify,
                catalog_rescan_state=catalog_rescan_state,
                notify_admin=notify_admin,
            )

    application.bot_data["sync_institute_link_fn"] = sync_institute_link_fn

    scheduler_holder: dict = {}

    def reschedule_institute_link_jobs_cb(institute_link_id: int) -> None:
        lock = link_locks.setdefault(institute_link_id, asyncio.Lock())
        reschedule_institute_link_jobs(
            scheduler_holder["scheduler"], conn, config, application.bot, institute_link_id, week_formula_holder, lock,
            catalog_rescan_state,
        )

    def reschedule_institute_jobs_cb(institute_id: int) -> None:
        reschedule_institute_jobs(
            scheduler_holder["scheduler"], conn, config, application.bot, institute_id, week_formula_holder, link_locks,
            catalog_rescan_state,
        )

    application.bot_data["reschedule_institute_link_jobs"] = reschedule_institute_link_jobs_cb
    application.bot_data["reschedule_institute_jobs"] = reschedule_institute_jobs_cb

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("subscribe", subscribe_command))
    application.add_handler(CommandHandler("unsubscribe", unsubscribe_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CommandHandler("classmates", classmates_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CallbackQueryHandler(day_callback, pattern=r"^day:\d{4}-\d{2}-\d{2}$"))
    application.add_handler(CallbackQueryHandler(noop_callback, pattern=f"^{NOOP_CALLBACK_DATA}$"))
    application.add_handler(CallbackQueryHandler(institute_select_callback, pattern=INSTITUTE_SELECT_CALLBACK_PATTERN))
    application.add_handler(CallbackQueryHandler(institute_page_callback, pattern=INSTITUTE_PAGE_CALLBACK_PATTERN))
    application.add_handler(CallbackQueryHandler(group_select_callback, pattern=SELECT_CALLBACK_PATTERN))
    application.add_handler(CallbackQueryHandler(group_page_callback, pattern=PAGE_CALLBACK_PATTERN))
    application.add_handler(CommandHandler("add_institute", add_institute_command))
    application.add_handler(CommandHandler("set_institute_catalog_url", set_institute_catalog_url_command))
    application.add_handler(CommandHandler("set_institute_check_interval", set_institute_check_interval_command))
    application.add_handler(CommandHandler("set_institute_send_time", set_institute_send_time_command))
    application.add_handler(CommandHandler("set_institute_catalog_interval", set_institute_catalog_interval_command))
    application.add_handler(CommandHandler("refresh_institute_links", refresh_institute_links_command))
    application.add_handler(CommandHandler("institute_links", institute_links_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("alert", alert_command))

    application.add_handler(CommandHandler("starosta", starosta_command))
    application.add_handler(CommandHandler("votes", votes_command))
    application.add_handler(CommandHandler("change_group", change_group_command))
    application.add_handler(CommandHandler("set_starosta", set_starosta_command))
    application.add_handler(CommandHandler("remove_starosta", remove_starosta_command))

    application.add_handler(CallbackQueryHandler(nominate_self_callback, pattern=r"^starosta:nominate_self$"))
    application.add_handler(CallbackQueryHandler(starosta_change_callback, pattern=r"^starosta:change$"))
    application.add_handler(CallbackQueryHandler(starcand_page_callback, pattern=r"^starcand:page:\d+$"))
    application.add_handler(CallbackQueryHandler(starcand_select_callback, pattern=r"^starcand:select:\d+$"))
    application.add_handler(CallbackQueryHandler(starcand_abort_callback, pattern=r"^starcand:abort$"))
    application.add_handler(CallbackQueryHandler(starcand_confirm_callback, pattern=r"^starcand:confirm:\d+$"))
    application.add_handler(CallbackQueryHandler(election_approve_callback, pattern=r"^election:approve:\d+$"))
    application.add_handler(CallbackQueryHandler(election_reject_callback, pattern=r"^election:reject:\d+$"))

    application.add_handler(CallbackQueryHandler(change_group_page_callback, pattern=r"^chggrp:page:\d+$"))
    application.add_handler(CallbackQueryHandler(change_group_select_callback, pattern=r"^chggrp:select:\d+$"))
    application.add_handler(CallbackQueryHandler(change_group_abort_callback, pattern=r"^chgabort$"))
    application.add_handler(CallbackQueryHandler(change_group_confirm_callback, pattern=r"^chgconfirm:\d+$"))
    application.add_handler(CallbackQueryHandler(change_request_approve_callback, pattern=r"^chgreq:approve:\d+$"))
    application.add_handler(CallbackQueryHandler(change_request_reject_callback, pattern=r"^chgreq:reject:\d+$"))

    application.add_handler(CommandHandler("close_group", close_group_command))
    application.add_handler(CommandHandler("open_group", open_group_command))
    application.add_handler(CommandHandler("notify", notify_command))
    application.add_handler(CommandHandler("requests", requests_command))

    application.add_handler(CallbackQueryHandler(exclude_select_callback, pattern=r"^exclude:select:\d+$"))
    application.add_handler(CallbackQueryHandler(exclude_confirm_callback, pattern=r"^exclude:confirm:\d+$"))
    application.add_handler(CallbackQueryHandler(exclude_abort_callback, pattern=r"^exclude:abort$"))

    application.add_handler(CommandHandler("menu", menu_command))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:"))
    application.add_handler(CommandHandler("report", report_command))
    application.add_handler(CommandHandler("faq", faq_command))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, pending_action_followup))

    # Pass the SAME link_locks dict used by sync_institute_link_fn/
    # reschedule_institute_link_jobs_cb above, so every link that already
    # exists at startup gets its scheduled jobs wired to the identical
    # Lock object a manual /refresh for that link will use — not a
    # separate, internally-created lock.
    scheduler_holder["scheduler"] = build_scheduler(
        conn, config, application.bot, week_formula_holder, locks=link_locks,
        catalog_rescan_state=catalog_rescan_state,
    )
    # Exposed so tests (and any future consumer) can assert the scheduled
    # jobs and the manual /refresh path really share ONE rate-limit dict.
    application.bot_data["_catalog_rescan_state"] = catalog_rescan_state
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

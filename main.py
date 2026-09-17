import os
import sys

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from schedule_bot import storage
from schedule_bot.bot import (
    alert_command,
    day_callback,
    refresh_command,
    schedule_command,
    set_check_interval_command,
    set_group_command,
    set_pdf_source_command,
    set_send_time_command,
    start_command,
    stop_command,
)
from schedule_bot.config import Config, load_config
from schedule_bot.scheduler import (
    WeekFormulaHolder,
    build_scheduler,
    reschedule_daily_job,
    reschedule_hourly_job,
    resolve_sync_settings,
)
from schedule_bot.sync import check_and_apply_diff
from schedule_bot.weektype import WeekFormula, fetch_week_formula, load_week_formula, save_week_formula

_FALLBACK_WEEK_FORMULA = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)


class _LiveWeekFormula:
    """Delegates to a WeekFormulaHolder so bot_data always sees the latest
    formula after the weekly refresh job mutates the holder in place —
    storing `holder.formula` directly would freeze a stale snapshot."""

    def __init__(self, holder: WeekFormulaHolder) -> None:
        self._holder = holder

    def compute(self, on_date):
        return self._holder.formula.compute(on_date)


def build_application(config: Config, conn) -> Application:
    storage.ensure_default_settings(
        conn,
        {
            "pdf_source": config.default_pdf_source,
            "group_name": config.default_group_name,
            "weektype_url": config.default_weektype_url,
            "daily_send_time": config.default_daily_send_time,
            "check_interval_minutes": str(config.default_check_interval_minutes),
            "timezone": config.default_timezone,
        },
    )

    week_formula = _fetch_week_formula_or_fallback(conn, storage.get_setting(conn, "weektype_url"))
    week_formula_holder = WeekFormulaHolder(formula=week_formula)

    application = Application.builder().token(config.telegram_token).build()
    application.bot_data["conn"] = conn
    application.bot_data["admin_chat_id"] = config.admin_chat_id
    application.bot_data["group_name"] = storage.get_setting(conn, "group_name")
    application.bot_data["_week_formula_holder"] = week_formula_holder
    application.bot_data["week_formula"] = _LiveWeekFormula(week_formula_holder)
    application.bot_data["active_schedule_getter"] = lambda: _load_active_schedule(conn)
    application.bot_data["active_pdf_path_getter"] = lambda: _load_active_pdf_path(conn)

    async def sync_fn():
        pdf_source, group_name = resolve_sync_settings(conn, config)

        async def notify(chat_id, text):
            await application.bot.send_message(chat_id=chat_id, text=text)

        return await check_and_apply_diff(conn, pdf_source, group_name, config.pdf_cache_dir, notify)

    application.bot_data["sync_fn"] = sync_fn

    scheduler_holder: dict = {}

    def reschedule_daily():
        reschedule_daily_job(
            scheduler_holder["scheduler"], conn, storage.get_setting(conn, "timezone"), config
        )

    def reschedule_hourly():
        reschedule_hourly_job(scheduler_holder["scheduler"], conn, config)

    application.bot_data["reschedule_daily_job"] = reschedule_daily
    application.bot_data["reschedule_hourly_job"] = reschedule_hourly

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CallbackQueryHandler(day_callback, pattern=r"^day:\d+$"))
    application.add_handler(CommandHandler("set_pdf_source", set_pdf_source_command))
    application.add_handler(CommandHandler("set_group", set_group_command))
    application.add_handler(CommandHandler("set_send_time", set_send_time_command))
    application.add_handler(CommandHandler("set_check_interval", set_check_interval_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("alert", alert_command))

    scheduler_holder["scheduler"] = build_scheduler(conn, config, application.bot, week_formula_holder)
    application.bot_data["_scheduler"] = scheduler_holder["scheduler"]

    return application


def _load_active_schedule(conn):
    import json

    from schedule_bot.pdf_parser import schedule_from_dict

    group_name = storage.get_setting(conn, "group_name")
    active = storage.get_active_version(conn, group_name)
    if active is None:
        return None
    return schedule_from_dict(json.loads(active.payload_json))


def _fetch_week_formula_or_fallback(conn, url):
    """Fetch the week-type formula from the institute's site. On any
    failure (network error, or the expected pattern not found in the
    page), fall back to the last persisted formula, and if that's not
    available either, to a hardcoded last-resort default — never let the
    exception propagate and crash application startup."""
    try:
        formula = fetch_week_formula(url)
        save_week_formula(conn, formula)
        return formula
    except Exception as error:
        persisted = load_week_formula(conn)
        if persisted is not None:
            print(
                f"main: failed to fetch week formula from {url!r} ({error}); "
                "falling back to last persisted formula",
                file=sys.stderr,
            )
            return persisted
        print(
            f"main: failed to fetch week formula from {url!r} ({error}); "
            "no persisted formula available, falling back to hardcoded default",
            file=sys.stderr,
        )
        return _FALLBACK_WEEK_FORMULA


def _load_active_pdf_path(conn):
    group_name = storage.get_setting(conn, "group_name")
    active = storage.get_active_version(conn, group_name)
    if active is None:
        return None
    return active.pdf_cache_path


if __name__ == "__main__":
    import sqlite3

    config = load_config(os.environ)
    connection = sqlite3.connect(config.db_path)
    storage.init_db(connection)
    app = build_application(config, connection)
    app.run_polling()

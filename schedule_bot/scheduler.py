import asyncio
import json
from dataclasses import dataclass
from datetime import date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from schedule_bot import storage
from schedule_bot.formatting import format_day_schedule
from schedule_bot.pdf_parser import DAY_ORDER, schedule_from_dict
from schedule_bot.sync import check_and_apply_diff
from schedule_bot.weektype import WeekFormula, fetch_week_formula


@dataclass
class WeekFormulaHolder:
    formula: WeekFormula


async def _notify(bot, chat_id: int, text: str) -> None:
    await bot.send_message(chat_id=chat_id, text=text)


async def _run_sync(conn, config, bot) -> "SyncResult":
    pdf_source = storage.get_setting(conn, "pdf_source") or config.default_pdf_source
    group_name = storage.get_setting(conn, "group_name") or config.default_group_name
    return await check_and_apply_diff(
        conn, pdf_source, group_name, config.pdf_cache_dir, lambda chat_id, text: _notify(bot, chat_id, text)
    )


async def hourly_check_job(conn, config, bot) -> None:
    await _run_sync(conn, config, bot)


async def daily_send_job(conn, config, bot, week_formula_holder: WeekFormulaHolder) -> None:
    await _run_sync(conn, config, bot)

    group_name = storage.get_setting(conn, "group_name") or config.default_group_name
    active = storage.get_active_version(conn, group_name)
    if active is None:
        return
    schedule = schedule_from_dict(json.loads(active.payload_json))

    today = date.today()
    day_name = DAY_ORDER[today.weekday()] if today.weekday() < len(DAY_ORDER) else None
    if day_name is None:
        return  # Sunday: no classes in this schedule

    week_type = week_formula_holder.formula.compute(today)
    text = format_day_schedule(day_name, today, week_type, schedule)

    for chat_id in storage.list_subscribers(conn, group_name):
        await bot.send_message(chat_id=chat_id, text=text)


async def weekly_weektype_refresh_job(conn, week_formula_holder: WeekFormulaHolder) -> None:
    url = storage.get_setting(conn, "weektype_url")
    if not url:
        return
    try:
        week_formula_holder.formula = fetch_week_formula(url)
    except Exception:
        pass  # keep using the last known-good formula; caller may log this


def _parse_hh_mm(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.split(":", 1)
    return int(hour_str), int(minute_str)


def build_scheduler(conn, config, bot, week_formula_holder: WeekFormulaHolder) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=storage.get_setting(conn, "timezone") or config.default_timezone)

    send_time = storage.get_setting(conn, "daily_send_time") or config.default_daily_send_time
    hour, minute = _parse_hh_mm(send_time)
    scheduler.add_job(
        daily_send_job,
        CronTrigger(hour=hour, minute=minute),
        args=[conn, config, bot, week_formula_holder],
        id="daily_send",
        replace_existing=True,
    )

    interval_minutes = int(storage.get_setting(conn, "check_interval_minutes") or config.default_check_interval_minutes)
    scheduler.add_job(
        hourly_check_job,
        IntervalTrigger(minutes=interval_minutes),
        args=[conn, config, bot],
        id="hourly_check",
        replace_existing=True,
    )

    scheduler.add_job(
        weekly_weektype_refresh_job,
        CronTrigger(day_of_week="mon", hour=6, minute=0),
        args=[conn, week_formula_holder],
        id="weekly_weektype_refresh",
        replace_existing=True,
    )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    scheduler.start()
    return scheduler


def reschedule_daily_job(scheduler: AsyncIOScheduler, conn, timezone_name: str) -> None:
    send_time = storage.get_setting(conn, "daily_send_time")
    hour, minute = _parse_hh_mm(send_time)
    scheduler.reschedule_job("daily_send", trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone_name))


def reschedule_hourly_job(scheduler: AsyncIOScheduler, conn) -> None:
    interval_minutes = int(storage.get_setting(conn, "check_interval_minutes"))
    scheduler.reschedule_job("hourly_check", trigger=IntervalTrigger(minutes=interval_minutes))

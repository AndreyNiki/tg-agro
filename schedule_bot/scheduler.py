import asyncio
import json
import sys
from dataclasses import dataclass
from datetime import date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from schedule_bot import storage
from schedule_bot.formatting import format_day_schedule
from schedule_bot.pdf_parser import DAY_ORDER, schedule_from_dict
from schedule_bot.screenshot import render_day_image
from schedule_bot.sync import check_and_apply_diff
from schedule_bot.weektype import WeekFormula, fetch_week_formula, save_week_formula


@dataclass
class WeekFormulaHolder:
    formula: WeekFormula


async def _notify(bot, chat_id: int, text: str) -> None:
    await bot.send_message(chat_id=chat_id, text=text)


def resolve_sync_settings(conn, config) -> tuple[str, str]:
    """Resolve (pdf_source, group_name) from stored settings, falling back
    to config defaults if a setting is missing (should not normally
    happen since ensure_default_settings seeds them, but is a real risk
    if a setting is ever deleted or the DB is manually edited)."""
    pdf_source = storage.get_setting(conn, "pdf_source") or config.default_pdf_source
    group_name = storage.get_setting(conn, "group_name") or config.default_group_name
    return pdf_source, group_name


async def _run_sync(conn, config, bot) -> "SyncResult":
    pdf_source, group_name = resolve_sync_settings(conn, config)
    return await check_and_apply_diff(
        conn, pdf_source, group_name, config.pdf_cache_dir, lambda chat_id, text: _notify(bot, chat_id, text)
    )


async def hourly_check_job(conn, config, bot) -> None:
    await _run_sync(conn, config, bot)


async def daily_send_job(conn, config, bot, week_formula_holder: WeekFormulaHolder) -> None:
    # I1: sync failure must not prevent the daily send — subscribers should
    # get today's schedule "в любом случае" even if the sync check errors.
    try:
        await _run_sync(conn, config, bot)
    except Exception as error:
        print(f"scheduler: daily_send_job sync failed, sending with last known schedule: {error}", file=sys.stderr)

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

    # I3: render the day's screenshot once and reuse the bytes for every
    # subscriber; I4: a missing/stale cached PDF must not crash the job.
    image_bytes = None
    try:
        image_bytes = render_day_image(active.pdf_cache_path, schedule, day_name)
    except Exception as error:
        print(f"scheduler: failed to render day image for {active.pdf_cache_path!r}: {error}", file=sys.stderr)

    # I2: isolate per-subscriber failures so one blocked chat doesn't kill
    # the daily send for everyone after it.
    for chat_id in storage.list_subscribers(conn, group_name):
        try:
            await bot.send_message(chat_id=chat_id, text=text)
            if image_bytes is not None:
                await bot.send_photo(chat_id=chat_id, photo=image_bytes)
        except Exception as error:
            print(f"scheduler: failed to send daily schedule to chat_id={chat_id}: {error}", file=sys.stderr)


async def weekly_weektype_refresh_job(conn, week_formula_holder: WeekFormulaHolder) -> None:
    url = storage.get_setting(conn, "weektype_url")
    if not url:
        return
    try:
        new_formula = fetch_week_formula(url)
    except Exception as error:
        print(
            f"scheduler: weekly week-type refresh failed for {url!r} ({error}); "
            "keeping last known-good formula",
            file=sys.stderr,
        )
        return
    week_formula_holder.formula = new_formula
    save_week_formula(conn, new_formula)


def _parse_hh_mm(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.split(":", 1)
    return int(hour_str), int(minute_str)


def _safe_hh_mm(value: str, default: str) -> tuple[int, int]:
    """Parse an HH:MM setting, falling back to `default` (and logging a
    warning) if the stored value is somehow malformed. Defense in depth
    for values written to the DB before admin-input validation existed,
    or edited directly in the SQLite file."""
    try:
        return _parse_hh_mm(value)
    except (ValueError, AttributeError) as error:
        print(
            f"scheduler: invalid daily_send_time setting {value!r} ({error}); "
            f"falling back to default {default!r}",
            file=sys.stderr,
        )
        return _parse_hh_mm(default)


def _safe_interval_minutes(value: str, default: int) -> int:
    """Parse a check-interval setting, falling back to `default` (and
    logging a warning) if the stored value is somehow malformed."""
    try:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("interval must be positive")
        return parsed
    except (ValueError, TypeError) as error:
        print(
            f"scheduler: invalid check_interval_minutes setting {value!r} ({error}); "
            f"falling back to default {default!r}",
            file=sys.stderr,
        )
        return int(default)


def build_scheduler(conn, config, bot, week_formula_holder: WeekFormulaHolder) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=storage.get_setting(conn, "timezone") or config.default_timezone)

    send_time = storage.get_setting(conn, "daily_send_time") or config.default_daily_send_time
    hour, minute = _safe_hh_mm(send_time, config.default_daily_send_time)
    scheduler.add_job(
        daily_send_job,
        CronTrigger(hour=hour, minute=minute),
        args=[conn, config, bot, week_formula_holder],
        id="daily_send",
        replace_existing=True,
    )

    interval_minutes = _safe_interval_minutes(
        storage.get_setting(conn, "check_interval_minutes") or config.default_check_interval_minutes,
        config.default_check_interval_minutes,
    )
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


def reschedule_daily_job(scheduler: AsyncIOScheduler, conn, timezone_name: str, config=None) -> None:
    send_time = storage.get_setting(conn, "daily_send_time")
    default = config.default_daily_send_time if config is not None else "09:00"
    hour, minute = _safe_hh_mm(send_time, default)
    scheduler.reschedule_job("daily_send", trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone_name))


def reschedule_hourly_job(scheduler: AsyncIOScheduler, conn, config=None) -> None:
    interval_minutes_setting = storage.get_setting(conn, "check_interval_minutes")
    default = config.default_check_interval_minutes if config is not None else 60
    interval_minutes = _safe_interval_minutes(interval_minutes_setting, default)
    scheduler.reschedule_job("hourly_check", trigger=IntervalTrigger(minutes=interval_minutes))

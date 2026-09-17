import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from schedule_bot import storage
from schedule_bot.bot import STALE_SCHEDULE_WARNING
from schedule_bot.formatting import format_day_schedule
from schedule_bot.pdf_parser import DAY_ORDER, schedule_from_dict
from schedule_bot.screenshot import render_day_image
from schedule_bot.sync import check_and_apply_diff
from schedule_bot.telegram_utils import SEND_PACING_SECONDS, send_with_retry
from schedule_bot.timeutil import today_in_configured_timezone
from schedule_bot.weektype import WeekFormula, fetch_week_formula, save_week_formula

logger = logging.getLogger(__name__)


@dataclass
class WeekFormulaHolder:
    formula: WeekFormula


async def _notify(bot, chat_id: int, text: str) -> None:
    await send_with_retry(lambda: bot.send_message(chat_id=chat_id, text=text))


def resolve_sync_settings(conn, config) -> tuple[str, str]:
    """Resolve (pdf_source, group_name) from stored settings, falling back
    to config defaults if a setting is missing (should not normally
    happen since ensure_default_settings seeds them, but is a real risk
    if a setting is ever deleted or the DB is manually edited)."""
    pdf_source = storage.get_setting(conn, "pdf_source") or config.default_pdf_source
    group_name = storage.get_setting(conn, "group_name") or config.default_group_name
    return pdf_source, group_name


async def _run_sync(conn, config, bot, lock: asyncio.Lock | None = None) -> "SyncResult":
    pdf_source, group_name = resolve_sync_settings(conn, config)

    async def _do_sync():
        return await check_and_apply_diff(
            conn, pdf_source, group_name, config.pdf_cache_dir, lambda chat_id, text: _notify(bot, chat_id, text)
        )

    # Guard against hourly_check_job, daily_send_job and a manual /refresh
    # overlapping: without this, two concurrent syncs can both diff against
    # the same "active" row, both apply, double-broadcast the change, and
    # both try to delete the same superseded cache file — orphaning the
    # second sync's PDF on disk permanently (the only GC mechanism for
    # pdf_cache_dir is the one in sync.py that assumes a single writer).
    if lock is None:
        return await _do_sync()
    async with lock:
        return await _do_sync()


async def hourly_check_job(conn, config, bot, lock: asyncio.Lock | None = None) -> None:
    logger.info("scheduler: hourly_check_job starting")
    # A fetch/parse failure (network blip, site down/slow) is expected and
    # self-healing — the next interval retries automatically, and
    # check_and_apply_diff has already logged the details and recorded
    # last_sync_failed_at. Catch it here (like daily_send_job does) so
    # APScheduler doesn't treat it as the job crashing and dump a full
    # traceback at ERROR level for what is a routine, already-handled
    # condition.
    try:
        result = await _run_sync(conn, config, bot, lock)
    except Exception as error:
        logger.warning("scheduler: hourly_check_job sync failed: %s", error)
        # Unlike a manual /refresh, which replies to whoever ran it, a
        # failed hourly check previously left the admin unaware the
        # schedule could be stale unless they checked server logs.
        try:
            await send_with_retry(
                lambda: bot.send_message(
                    chat_id=config.admin_chat_id,
                    text=f"Ежечасная проверка расписания не удалась: {error}",
                )
            )
        except Exception as notify_error:
            logger.warning(
                "scheduler: hourly_check_job failed to notify admin: %s", notify_error
            )
        return
    logger.info("scheduler: hourly_check_job finished with status=%s", result.status)


async def daily_send_job(
    conn, config, bot, week_formula_holder: WeekFormulaHolder, lock: asyncio.Lock | None = None
) -> None:
    logger.info("scheduler: daily_send_job starting")
    # I1: sync failure must not prevent the daily send — subscribers should
    # get today's schedule "в любом случае" even if the sync check errors.
    try:
        await _run_sync(conn, config, bot, lock)
    except Exception as error:
        logger.warning(
            "scheduler: daily_send_job sync failed, sending with last known schedule: %s", error
        )

    # I8/consistency: resolve group_name the same way every sync entry
    # point does, instead of duplicating the fallback logic here.
    _, group_name = resolve_sync_settings(conn, config)
    active = storage.get_active_version(conn, group_name)
    if active is None:
        return
    schedule = schedule_from_dict(json.loads(active.payload_json))

    today = today_in_configured_timezone(conn)
    day_name = DAY_ORDER[today.weekday()] if today.weekday() < len(DAY_ORDER) else None
    if day_name is None:
        return  # Sunday: no classes in this schedule

    week_type = week_formula_holder.formula.compute(today)
    text = format_day_schedule(day_name, today, week_type, schedule)
    if storage.get_setting(conn, "last_sync_failed_at"):
        text = STALE_SCHEDULE_WARNING + text

    # I3: render the day's screenshot once and reuse the bytes for every
    # subscriber; I4: a missing/stale cached PDF must not crash the job.
    image_bytes = None
    try:
        image_bytes = render_day_image(active.pdf_cache_path, schedule, day_name)
    except Exception as error:
        logger.warning(
            "scheduler: failed to render day image for %r: %s", active.pdf_cache_path, error
        )

    # I2: isolate per-subscriber failures so one blocked chat doesn't kill
    # the daily send for everyone after it.
    subscribers = storage.list_subscribers(conn, group_name)
    sent = 0
    for chat_id in subscribers:
        try:
            await send_with_retry(lambda chat_id=chat_id: bot.send_message(chat_id=chat_id, text=text))
            if image_bytes is not None:
                await send_with_retry(
                    lambda chat_id=chat_id: bot.send_photo(chat_id=chat_id, photo=image_bytes)
                )
            sent += 1
        except Exception as error:
            logger.warning(
                "scheduler: failed to send daily schedule to chat_id=%s: %s", chat_id, error
            )
        await asyncio.sleep(SEND_PACING_SECONDS)
    logger.info("scheduler: daily_send_job sent to %d/%d subscribers", sent, len(subscribers))


async def weekly_weektype_refresh_job(conn, week_formula_holder: WeekFormulaHolder) -> None:
    logger.info("scheduler: weekly_weektype_refresh_job starting")
    url = storage.get_setting(conn, "weektype_url")
    if not url:
        return
    try:
        # fetch_week_formula does a blocking requests.get; run it off the
        # event loop so it doesn't stall the bot's own polling/HTTP traffic.
        new_formula = await asyncio.to_thread(fetch_week_formula, url)
    except Exception as error:
        logger.warning(
            "scheduler: weekly week-type refresh failed for %r (%s); keeping last known-good formula",
            url,
            error,
        )
        return
    week_formula_holder.formula = new_formula
    save_week_formula(conn, new_formula)
    logger.info("scheduler: weekly_weektype_refresh_job updated formula from %r", url)


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
        logger.warning(
            "scheduler: invalid daily_send_time setting %r (%s); falling back to default %r",
            value,
            error,
            default,
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
        logger.warning(
            "scheduler: invalid check_interval_minutes setting %r (%s); falling back to default %r",
            value,
            error,
            default,
        )
        return int(default)


def build_scheduler(
    conn, config, bot, week_formula_holder: WeekFormulaHolder, lock: asyncio.Lock | None = None
) -> AsyncIOScheduler:
    if lock is None:
        lock = asyncio.Lock()

    tz_name = storage.get_setting(conn, "timezone") or config.default_timezone
    scheduler = AsyncIOScheduler(timezone=tz_name)

    send_time = storage.get_setting(conn, "daily_send_time") or config.default_daily_send_time
    hour, minute = _safe_hh_mm(send_time, config.default_daily_send_time)
    scheduler.add_job(
        daily_send_job,
        # A CronTrigger built without timezone= binds to the local system
        # timezone at construction time, NOT the scheduler's own timezone —
        # AsyncIOScheduler(timezone=...) only applies to triggers that don't
        # specify their own. Passing tz_name explicitly here is what makes
        # the daily send actually fire at TIMEZONE-local hour:minute rather
        # than the container's (usually UTC) local time.
        CronTrigger(hour=hour, minute=minute, timezone=tz_name),
        args=[conn, config, bot, week_formula_holder, lock],
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
        args=[conn, config, bot, lock],
        id="hourly_check",
        replace_existing=True,
    )

    scheduler.add_job(
        weekly_weektype_refresh_job,
        CronTrigger(day_of_week="mon", hour=6, minute=0, timezone=tz_name),
        args=[conn, week_formula_holder],
        id="weekly_weektype_refresh",
        replace_existing=True,
    )

    # A fresh install (or a DB wiped between restarts) has no active
    # schedule until the first hourly_check_job fires — which, with
    # IntervalTrigger, is `interval_minutes` after startup, not on
    # startup. Run the same sync once immediately so /schedule works
    # right after the bot comes up.
    scheduler.add_job(
        hourly_check_job,
        DateTrigger(run_date=datetime.now()),
        args=[conn, config, bot, lock],
        id="startup_sync",
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

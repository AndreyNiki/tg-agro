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


async def _run_sync(conn, config, bot, group, lock: asyncio.Lock | None = None):
    async def _do_sync():
        return await check_and_apply_diff(
            conn, group, config.pdf_cache_dir, lambda chat_id, text: _notify(bot, chat_id, text)
        )

    # Guard against hourly_check_job, daily_send_job and a manual /refresh
    # for the SAME group overlapping: without this, two concurrent syncs
    # for that one group can both diff against the same "active" row,
    # both apply, double-broadcast, and both try to delete the same
    # superseded cache file. Each group is given its OWN lock (see
    # build_scheduler), so this never serializes syncs ACROSS groups —
    # only within a single group's own jobs.
    if lock is None:
        return await _do_sync()
    async with lock:
        return await _do_sync()


async def hourly_check_job(conn, config, bot, group_id: int, lock: asyncio.Lock | None = None) -> None:
    group = storage.get_group(conn, group_id)
    if group is None:
        logger.warning("scheduler: hourly_check_job skipped, group_id=%s no longer exists", group_id)
        return
    logger.info("scheduler: hourly_check_job starting for group %r", group.name)
    try:
        result = await _run_sync(conn, config, bot, group, lock)
    except Exception as error:
        logger.warning("scheduler: hourly_check_job sync failed for group %r: %s", group.name, error)
        try:
            await send_with_retry(
                lambda: bot.send_message(
                    chat_id=config.admin_chat_id,
                    text=f"Ежечасная проверка расписания группы «{group.name}» не удалась: {error}",
                )
            )
        except Exception as notify_error:
            logger.warning("scheduler: hourly_check_job failed to notify admin: %s", notify_error)
        return
    logger.info("scheduler: hourly_check_job finished for group %r with status=%s", group.name, result.status)


async def daily_send_job(
    conn, config, bot, group_id: int, week_formula_holder: WeekFormulaHolder, lock: asyncio.Lock | None = None
) -> None:
    group = storage.get_group(conn, group_id)
    if group is None:
        logger.warning("scheduler: daily_send_job skipped, group_id=%s no longer exists", group_id)
        return
    logger.info("scheduler: daily_send_job starting for group %r", group.name)
    try:
        await _run_sync(conn, config, bot, group, lock)
    except Exception as error:
        logger.warning(
            "scheduler: daily_send_job sync failed for group %r, sending with last known schedule: %s",
            group.name, error,
        )

    active = storage.get_active_version(conn, group_id)
    if active is None:
        return
    schedule = schedule_from_dict(json.loads(active.payload_json))

    today = today_in_configured_timezone(group.timezone)
    day_name = DAY_ORDER[today.weekday()] if today.weekday() < len(DAY_ORDER) else None
    if day_name is None:
        return  # Sunday: no classes in this schedule

    week_type = week_formula_holder.formula.compute(today)
    text = format_day_schedule(day_name, today, week_type, schedule)
    group = storage.get_group(conn, group_id)  # re-fetch: _run_sync may have just set last_sync_failed_at
    if group is None:
        logger.warning("scheduler: daily_send_job group_id=%s deleted mid-send, aborting", group_id)
        return
    if group.last_sync_failed_at:
        text = STALE_SCHEDULE_WARNING + text

    image_bytes = None
    try:
        image_bytes = render_day_image(active.pdf_cache_path, schedule, day_name)
    except Exception as error:
        logger.warning("scheduler: failed to render day image for %r: %s", active.pdf_cache_path, error)

    members = storage.list_group_members(conn, group_id)
    sent = 0
    for chat_id in members:
        try:
            await send_with_retry(lambda chat_id=chat_id: bot.send_message(chat_id=chat_id, text=text))
            if image_bytes is not None:
                await send_with_retry(lambda chat_id=chat_id: bot.send_photo(chat_id=chat_id, photo=image_bytes))
            sent += 1
        except Exception as error:
            logger.warning("scheduler: failed to send daily schedule to chat_id=%s: %s", chat_id, error)
        await asyncio.sleep(SEND_PACING_SECONDS)
    logger.info("scheduler: daily_send_job for group %r sent to %d/%d subscribers", group.name, sent, len(members))


async def weekly_weektype_refresh_job(conn, week_formula_holder: WeekFormulaHolder) -> None:
    logger.info("scheduler: weekly_weektype_refresh_job starting")
    url = storage.get_setting(conn, "weektype_url")
    if not url:
        return
    try:
        new_formula = await asyncio.to_thread(fetch_week_formula, url)
    except Exception as error:
        logger.warning(
            "scheduler: weekly week-type refresh failed for %r (%s); keeping last known-good formula", url, error
        )
        return
    week_formula_holder.formula = new_formula
    save_week_formula(conn, new_formula)
    logger.info("scheduler: weekly_weektype_refresh_job updated formula from %r", url)


def _parse_hh_mm(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.split(":", 1)
    return int(hour_str), int(minute_str)


def _safe_hh_mm(value: str, default: str) -> tuple[int, int]:
    try:
        return _parse_hh_mm(value)
    except (ValueError, AttributeError) as error:
        logger.warning(
            "scheduler: invalid send_time %r (%s); falling back to default %r", value, error, default
        )
        return _parse_hh_mm(default)


def _safe_interval_minutes(value: int | str, default: int) -> int:
    try:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("interval must be positive")
        return parsed
    except (ValueError, TypeError) as error:
        logger.warning(
            "scheduler: invalid check_interval_minutes %r (%s); falling back to default %r", value, error, default
        )
        return int(default)


def _group_job_ids(group_id: int) -> tuple[str, str, str]:
    return f"daily_send_{group_id}", f"hourly_check_{group_id}", f"startup_sync_{group_id}"


def schedule_group_jobs(scheduler, conn, config, bot, group, week_formula_holder, lock) -> None:
    """Add (or replace) this group's job triggers. add_job(...,
    replace_existing=True) works whether the job already exists (an
    admin changed send_time/check_interval) or doesn't yet (a brand-new
    group) — so this one function covers both cases."""
    daily_id, hourly_id, startup_id = _group_job_ids(group.id)

    hour, minute = _safe_hh_mm(group.send_time, config.default_daily_send_time)
    scheduler.add_job(
        daily_send_job,
        CronTrigger(hour=hour, minute=minute, timezone=group.timezone),
        args=[conn, config, bot, group.id, week_formula_holder, lock],
        id=daily_id,
        replace_existing=True,
    )

    interval_minutes = _safe_interval_minutes(group.check_interval_minutes, config.default_check_interval_minutes)
    scheduler.add_job(
        hourly_check_job,
        IntervalTrigger(minutes=interval_minutes),
        args=[conn, config, bot, group.id, lock],
        id=hourly_id,
        replace_existing=True,
    )

    # A freshly created group has no active schedule until its first
    # hourly_check_job fires — run the same sync once immediately so
    # /schedule works right away for its first subscriber.
    scheduler.add_job(
        hourly_check_job,
        DateTrigger(run_date=datetime.now()),
        args=[conn, config, bot, group.id, lock],
        id=startup_id,
        replace_existing=True,
    )


def reschedule_group_jobs(scheduler, conn, config, bot, group_id: int, week_formula_holder, lock) -> None:
    group = storage.get_group(conn, group_id)
    if group is None:
        return
    schedule_group_jobs(scheduler, conn, config, bot, group, week_formula_holder, lock)


def build_scheduler(conn, config, bot, week_formula_holder: WeekFormulaHolder, lock: asyncio.Lock | None = None) -> AsyncIOScheduler:
    # Each group gets its OWN lock, never shared with another group's jobs —
    # a shared single lock would serialize sync across unrelated groups
    # (different `active` schedule_versions rows, different cache files),
    # which defeats the point of per-group job pairs. The `lock` parameter
    # is accepted for backward-compatible call sites only and is NOT reused
    # across groups here; each group_id gets its own lazily-created lock.
    locks: dict[int, asyncio.Lock] = {}

    scheduler = AsyncIOScheduler(timezone=config.default_timezone)

    for group in storage.list_all_groups(conn):
        group_lock = locks.setdefault(group.id, asyncio.Lock())
        schedule_group_jobs(scheduler, conn, config, bot, group, week_formula_holder, group_lock)

    scheduler.add_job(
        weekly_weektype_refresh_job,
        CronTrigger(day_of_week="mon", hour=6, minute=0, timezone=config.default_timezone),
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

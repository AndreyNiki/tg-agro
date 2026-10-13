import asyncio
import json
import logging
from dataclasses import dataclass

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from schedule_bot import storage
from schedule_bot.bot import STALE_SCHEDULE_WARNING
from schedule_bot.formatting import format_day_schedule
from schedule_bot.group_links import refresh_institute_catalog
from schedule_bot.pdf_parser import DAY_ORDER, schedule_from_dict
from schedule_bot.screenshot import render_day_image
from schedule_bot.sync import sync_institute_link
from schedule_bot.telegram_utils import SEND_PACING_SECONDS, send_with_retry
from schedule_bot.timeutil import today_in_configured_timezone
from schedule_bot.weektype import WeekFormula, fetch_week_formula, save_week_formula

logger = logging.getLogger(__name__)


@dataclass
class WeekFormulaHolder:
    formula: WeekFormula


async def _notify(bot, chat_id: int, text: str) -> None:
    await send_with_retry(lambda: bot.send_message(chat_id=chat_id, text=text))


async def link_check_job(
    conn, config, bot, institute_link_id: int, lock: asyncio.Lock | None = None,
    catalog_rescan_state: dict | None = None,
) -> None:
    institute_link = storage.get_institute_link(conn, institute_link_id)
    if institute_link is None:
        logger.warning("scheduler: link_check_job skipped, institute_link_id=%s no longer exists", institute_link_id)
        return
    institute = storage.get_institute(conn, institute_link.institute_id)
    if institute is None:
        return

    # Must be the dict owned by build_scheduler/main.py and shared by every
    # job of every link, otherwise the 404-rescan rate limit degenerates
    # into "once per link per call", i.e. no limit at all. The local
    # fallback below only exists for direct unit-test calls.
    rescan_state = catalog_rescan_state if catalog_rescan_state is not None else {}

    async def _do_sync():
        return await sync_institute_link(
            conn, institute_link, institute, config.pdf_cache_dir,
            lambda chat_id, text: _notify(bot, chat_id, text),
            catalog_rescan_state=rescan_state,
            notify_admin=lambda text: _notify(bot, config.admin_chat_id, text),
        )

    logger.info("scheduler: link_check_job starting for link id=%s", institute_link_id)
    try:
        if lock is None:
            await _do_sync()
        else:
            async with lock:
                await _do_sync()
    except Exception as error:
        logger.warning("scheduler: link_check_job sync failed for link id=%s: %s", institute_link_id, error)
        try:
            await send_with_retry(
                lambda: bot.send_message(
                    chat_id=config.admin_chat_id,
                    text=f"Ежечасная проверка расписания по ссылке (id={institute_link_id}) не удалась: {error}",
                )
            )
        except Exception as notify_error:
            logger.warning("scheduler: link_check_job failed to notify admin: %s", notify_error)


async def link_daily_send_job(
    conn, config, bot, institute_link_id: int, week_formula_holder: WeekFormulaHolder, lock: asyncio.Lock | None = None,
    catalog_rescan_state: dict | None = None,
) -> None:
    institute_link = storage.get_institute_link(conn, institute_link_id)
    if institute_link is None:
        logger.warning("scheduler: link_daily_send_job skipped, institute_link_id=%s no longer exists", institute_link_id)
        return
    institute = storage.get_institute(conn, institute_link.institute_id)
    if institute is None:
        return

    rescan_state = catalog_rescan_state if catalog_rescan_state is not None else {}

    async def _do_sync():
        return await sync_institute_link(
            conn, institute_link, institute, config.pdf_cache_dir,
            lambda chat_id, text: _notify(bot, chat_id, text),
            catalog_rescan_state=rescan_state,
            notify_admin=lambda text: _notify(bot, config.admin_chat_id, text),
        )

    try:
        if lock is None:
            await _do_sync()
        else:
            async with lock:
                await _do_sync()
    except Exception as error:
        logger.warning("scheduler: link_daily_send_job sync failed for link id=%s: %s", institute_link_id, error)

    today = today_in_configured_timezone(institute.timezone)
    day_name = DAY_ORDER[today.weekday()] if today.weekday() < len(DAY_ORDER) else None
    if day_name is None:
        return  # Sunday: no classes

    week_type = week_formula_holder.formula.compute(today)
    render_error_notified = False

    for group in storage.list_groups_for_link(conn, institute_link_id):
        active = storage.get_active_version(conn, group.id)
        if active is None:
            continue
        schedule = schedule_from_dict(json.loads(active.payload_json))

        text = format_day_schedule(day_name, today, week_type, schedule)
        group = storage.get_group(conn, group.id)  # re-fetch: sync may have just set last_sync_failed_at
        if group is None:
            continue
        if group.last_sync_failed_at:
            text = STALE_SCHEDULE_WARNING + text

        image_bytes = None
        try:
            image_bytes = render_day_image(active.pdf_cache_path, schedule, day_name)
        except Exception as error:
            logger.warning("scheduler: failed to render day image for %r: %s", active.pdf_cache_path, error)
            if not render_error_notified:
                render_error_notified = True
                try:
                    await send_with_retry(
                        lambda: bot.send_message(
                            chat_id=config.admin_chat_id,
                            text=f"Сборка изображения для группы «{group.name}» ({day_name}) не удалась: {error}",
                        )
                    )
                except Exception as notify_error:
                    logger.warning("scheduler: failed to notify admin about render failure: %s", notify_error)

        members = storage.list_group_members(conn, group.id)
        for chat_id in members:
            try:
                await send_with_retry(lambda chat_id=chat_id: bot.send_message(chat_id=chat_id, text=text))
                if image_bytes is not None:
                    await send_with_retry(lambda chat_id=chat_id: bot.send_photo(chat_id=chat_id, photo=image_bytes))
            except Exception as error:
                logger.warning("scheduler: failed to send daily schedule to chat_id=%s: %s", chat_id, error)
            await asyncio.sleep(SEND_PACING_SECONDS)


async def institute_catalog_refresh_job(conn, config, bot, institute_id: int, reschedule_links=None) -> None:
    institute = storage.get_institute(conn, institute_id)
    if institute is None:
        return

    alerts: list[str] = []
    try:
        await asyncio.to_thread(refresh_institute_catalog, conn, institute, notify_admin=alerts.append)
    except Exception as error:
        logger.warning("scheduler: institute_catalog_refresh_job failed for institute %r: %s", institute.name, error)
        return

    # A link discovered by THIS background scan has no link_check_*/
    # link_daily_send_* jobs yet; without this call it would stay unsynced
    # until the process restarts (the manual rescan paths —
    # /refresh_institute_links and the menu button — already reschedule).
    if reschedule_links is not None:
        try:
            reschedule_links(institute_id)
        except Exception as error:
            logger.warning(
                "scheduler: institute_catalog_refresh_job failed to (re)schedule link jobs for institute %r: %s",
                institute.name, error,
            )

    for text in alerts:
        try:
            await send_with_retry(lambda text=text: bot.send_message(chat_id=config.admin_chat_id, text=text))
        except Exception as error:
            logger.warning("scheduler: institute_catalog_refresh_job failed to notify admin: %s", error)


async def weekly_weektype_refresh_job(conn, week_formula_holder: WeekFormulaHolder) -> None:
    logger.info("scheduler: weekly_weektype_refresh_job starting")
    url = storage.get_setting(conn, "weektype_url")
    if not url:
        return
    try:
        new_formula = await asyncio.to_thread(fetch_week_formula, url)
    except Exception as error:
        logger.warning("scheduler: weekly week-type refresh failed for %r (%s); keeping last known-good formula", url, error)
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
        logger.warning("scheduler: invalid send_time %r (%s); falling back to default %r", value, error, default)
        return _parse_hh_mm(default)


def _safe_interval_minutes(value: int | str, default: int) -> int:
    try:
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("interval must be positive")
        return parsed
    except (ValueError, TypeError) as error:
        logger.warning("scheduler: invalid interval %r (%s); falling back to default %r", value, error, default)
        return int(default)


def schedule_link_jobs(
    scheduler, conn, config, bot, institute_link, institute, week_formula_holder, lock,
    catalog_rescan_state: dict | None = None,
) -> None:
    check_id = f"link_check_{institute_link.id}"
    daily_id = f"link_daily_send_{institute_link.id}"

    # ONE shared dict for every job of every link (and for main.py's manual
    # /refresh path) — passing None/a fresh set here is what made the
    # 404-rescan rate limit a no-op.
    if catalog_rescan_state is None:
        catalog_rescan_state = {}

    hour, minute = _safe_hh_mm(institute.send_time, config.default_daily_send_time)
    scheduler.add_job(
        link_daily_send_job,
        CronTrigger(hour=hour, minute=minute, timezone=institute.timezone),
        args=[conn, config, bot, institute_link.id, week_formula_holder, lock, catalog_rescan_state],
        id=daily_id,
        replace_existing=True,
    )

    interval_minutes = _safe_interval_minutes(institute.check_interval_minutes, config.default_check_interval_minutes)
    scheduler.add_job(
        link_check_job,
        IntervalTrigger(minutes=interval_minutes),
        args=[conn, config, bot, institute_link.id, lock, catalog_rescan_state],
        id=check_id,
        replace_existing=True,
    )


def schedule_institute_catalog_job(scheduler, conn, config, bot, institute, reschedule_links=None) -> None:
    interval_minutes = _safe_interval_minutes(
        institute.catalog_refresh_interval_minutes, config.default_catalog_refresh_interval_minutes
    )
    scheduler.add_job(
        institute_catalog_refresh_job,
        IntervalTrigger(minutes=interval_minutes),
        args=[conn, config, bot, institute.id, reschedule_links],
        id=f"institute_catalog_refresh_{institute.id}",
        replace_existing=True,
    )


def make_links_rescheduler(scheduler, conn, config, bot, week_formula_holder, locks: dict, catalog_rescan_state: dict):
    """Callback handed to institute_catalog_refresh_job so a link found by
    the BACKGROUND catalog scan immediately gets its own
    link_check_*/link_daily_send_* jobs, wired to the same per-link Lock
    and the same shared catalog_rescan_state as everything else."""

    def reschedule_links(institute_id: int) -> None:
        institute = storage.get_institute(conn, institute_id)
        if institute is None:
            return
        for institute_link in storage.list_institute_links(conn, institute_id, include_stale=False):
            link_lock = locks.setdefault(institute_link.id, asyncio.Lock())
            schedule_link_jobs(
                scheduler, conn, config, bot, institute_link, institute, week_formula_holder, link_lock,
                catalog_rescan_state,
            )

    return reschedule_links


def reschedule_institute_link_jobs(
    scheduler, conn, config, bot, institute_link_id: int, week_formula_holder, lock,
    catalog_rescan_state: dict | None = None,
) -> None:
    institute_link = storage.get_institute_link(conn, institute_link_id)
    if institute_link is None:
        return
    institute = storage.get_institute(conn, institute_link.institute_id)
    if institute is None:
        return
    schedule_link_jobs(
        scheduler, conn, config, bot, institute_link, institute, week_formula_holder, lock, catalog_rescan_state
    )


def reschedule_institute_jobs(
    scheduler, conn, config, bot, institute_id: int, week_formula_holder, locks: dict,
    catalog_rescan_state: dict | None = None,
) -> None:
    institute = storage.get_institute(conn, institute_id)
    if institute is None:
        return
    if catalog_rescan_state is None:
        catalog_rescan_state = {}
    schedule_institute_catalog_job(
        scheduler, conn, config, bot, institute,
        make_links_rescheduler(scheduler, conn, config, bot, week_formula_holder, locks, catalog_rescan_state),
    )
    for institute_link in storage.list_institute_links(conn, institute_id, include_stale=False):
        link_lock = locks.setdefault(institute_link.id, asyncio.Lock())
        schedule_link_jobs(
            scheduler, conn, config, bot, institute_link, institute, week_formula_holder, link_lock,
            catalog_rescan_state,
        )



def build_scheduler(
    conn, config, bot, week_formula_holder: WeekFormulaHolder, locks: dict | None = None,
    catalog_rescan_state: dict | None = None,
) -> AsyncIOScheduler:
    if locks is None:
        locks = {}
    if catalog_rescan_state is None:
        catalog_rescan_state = {}

    scheduler = AsyncIOScheduler(timezone=config.default_timezone)

    reschedule_links = make_links_rescheduler(
        scheduler, conn, config, bot, week_formula_holder, locks, catalog_rescan_state
    )
    for institute in storage.list_institutes(conn):
        schedule_institute_catalog_job(scheduler, conn, config, bot, institute, reschedule_links)
        for institute_link in storage.list_institute_links(conn, institute.id, include_stale=False):
            link_lock = locks.setdefault(institute_link.id, asyncio.Lock())
            schedule_link_jobs(
                scheduler, conn, config, bot, institute_link, institute, week_formula_holder, link_lock,
                catalog_rescan_state,
            )

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

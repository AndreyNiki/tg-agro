import asyncio
import json
import logging

from telegram import Update
from telegram.ext import ContextTypes

from schedule_bot import storage
from schedule_bot.bot import _is_valid_hh_mm, is_admin
from schedule_bot.group_links import refresh_institute_catalog
from schedule_bot.telegram_utils import parse_positive_int

logger = logging.getLogger(__name__)


def _resolve_admin_institute(conn, institute_id_str: str):
    institute_id = parse_positive_int(institute_id_str)
    if institute_id is None:
        return None
    return storage.get_institute(conn, institute_id)


async def create_institute_and_scan(
    conn, config, name: str, catalog_url: str, notify_admin, lock: asyncio.Lock | None = None,
) -> tuple[int, str]:
    """Creates the institute row and runs its first catalog scan.

    Shared by add_institute_command (command path) and menu.py's
    create-institute dialog so the two entry points can never drift. The
    blocking HTTP fetch inside refresh_institute_catalog is wrapped in
    asyncio.to_thread by the caller's convention elsewhere, but is done here
    directly since both callers need the same off-event-loop guarantee.
    """
    institute_id = storage.create_institute(
        conn, name=name, catalog_url=catalog_url,
        check_interval_minutes=config.default_check_interval_minutes,
        send_time=config.default_daily_send_time, tz_name=config.default_timezone,
        catalog_refresh_interval_minutes=config.default_catalog_refresh_interval_minutes,
    )
    try:
        # asyncio.to_thread: refresh_institute_catalog does a blocking HTTP
        # fetch (~15s timeout). With concurrent_updates(True) a direct call
        # would stall every other user's update for that whole time.
        async def _do_scan():
            await asyncio.to_thread(
                refresh_institute_catalog, conn, storage.get_institute(conn, institute_id), notify_admin=notify_admin
            )

        if lock is None:
            await _do_scan()
        else:
            async with lock:
                await _do_scan()
        link_count = len(storage.list_institute_links(conn, institute_id, include_stale=False))
        if link_count:
            scan_status = f"каталог просканирован, ссылок найдено: {link_count}"
        else:
            # Never report a bare success here: a 0-link scan (broken page,
            # unparsable labels) leaves the institute with nothing to sync.
            scan_status = "каталог просканирован, ссылок не найдено"
    except Exception as error:
        # Institute creation must succeed even if the catalog page is unreachable right now —
        # a later /refresh_institute_links or the background catalog job can retry the scan.
        logger.warning("institute_commands: initial catalog scan failed for institute_id=%s: %s", institute_id, error)
        scan_status = f"сканирование каталога не удалось: {error}"
    return institute_id, scan_status


async def add_institute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /add_institute from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /add_institute <название> <url_каталога>")
        return
    catalog_url = context.args[-1]
    name = " ".join(context.args[:-1])
    if not catalog_url.startswith(("http://", "https://")):
        await update.message.reply_text(
            "Использование: /add_institute <название> <url_каталога> — url должен начинаться с http:// или https://"
        )
        return
    conn = context.bot_data["conn"]
    config = context.bot_data["config"]
    alerts: list[str] = []
    institute_id, scan_status = await create_institute_and_scan(conn, config, name, catalog_url, alerts.append)
    context.bot_data["reschedule_institute_jobs"](institute_id)
    for text in alerts:
        await update.message.reply_text(text)
    await update.message.reply_text(f"Институт «{name}» создан (id={institute_id}), {scan_status}.")


async def set_institute_catalog_url_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /set_institute_catalog_url from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_institute_catalog_url <institute_id> <url>")
        return
    conn = context.bot_data["conn"]
    institute = _resolve_admin_institute(conn, context.args[0])
    if institute is None:
        await update.message.reply_text("Институт с таким id не найден.")
        return
    storage.update_institute_catalog_url(conn, institute.id, context.args[1])
    await update.message.reply_text(f"Каталог института «{institute.name}» обновлён: {context.args[1]}")


async def set_institute_check_interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /set_institute_check_interval from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_institute_check_interval <institute_id> <минуты>")
        return
    conn = context.bot_data["conn"]
    institute = _resolve_admin_institute(conn, context.args[0])
    if institute is None:
        await update.message.reply_text("Институт с таким id не найден.")
        return
    try:
        minutes = int(context.args[1])
        if minutes <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
        return
    storage.update_institute_check_interval(conn, institute.id, minutes)
    for institute_link in storage.list_institute_links(conn, institute.id, include_stale=False):
        context.bot_data["reschedule_institute_link_jobs"](institute_link.id)
    await update.message.reply_text(f"Интервал проверки института «{institute.name}» обновлён: {minutes} мин.")


async def set_institute_send_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /set_institute_send_time from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_institute_send_time <institute_id> <HH:MM>")
        return
    conn = context.bot_data["conn"]
    institute = _resolve_admin_institute(conn, context.args[0])
    if institute is None:
        await update.message.reply_text("Институт с таким id не найден.")
        return
    if not _is_valid_hh_mm(context.args[1]):
        await update.message.reply_text("Неверный формат времени, используйте HH:MM.")
        return
    storage.update_institute_send_time(conn, institute.id, context.args[1])
    for institute_link in storage.list_institute_links(conn, institute.id, include_stale=False):
        context.bot_data["reschedule_institute_link_jobs"](institute_link.id)
    await update.message.reply_text(f"Время рассылки института «{institute.name}» обновлено: {context.args[1]}")


async def set_institute_catalog_interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /set_institute_catalog_interval from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_institute_catalog_interval <institute_id> <минуты>")
        return
    conn = context.bot_data["conn"]
    institute = _resolve_admin_institute(conn, context.args[0])
    if institute is None:
        await update.message.reply_text("Институт с таким id не найден.")
        return
    try:
        minutes = int(context.args[1])
        if minutes <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
        return
    storage.update_institute_catalog_refresh_interval(conn, institute.id, minutes)
    context.bot_data["reschedule_institute_jobs"](institute.id)
    await update.message.reply_text(f"Интервал сканирования каталога института «{institute.name}» обновлён: {minutes} мин.")


async def refresh_institute_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /refresh_institute_links from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /refresh_institute_links <institute_id>")
        return
    conn = context.bot_data["conn"]
    institute = _resolve_admin_institute(conn, context.args[0])
    if institute is None:
        await update.message.reply_text("Институт с таким id не найден.")
        return
    from schedule_bot.sync import catalog_lock_for

    lock = catalog_lock_for(context.bot_data["_catalog_locks"], institute.id)
    alerts = []
    try:
        async with lock:
            await asyncio.to_thread(refresh_institute_catalog, conn, institute, notify_admin=alerts.append)
    except Exception as error:
        await update.message.reply_text(f"Сканирование каталога не удалось: {error}")
        return
    context.bot_data["reschedule_institute_jobs"](institute.id)
    for text in alerts:
        await update.message.reply_text(text)
    await update.message.reply_text(f"Каталог института «{institute.name}» пересканирован.")


def format_institute_links(conn, institute_id: int) -> str:
    links = storage.list_institute_links(conn, institute_id)
    if not links:
        return "У этого института пока нет ссылок."
    lines = []
    for link in links:
        group_count = len(json.loads(link.group_names_json))
        stale_marker = " [stale]" if link.is_stale else ""
        lines.append(f"{link.raw_label!r} -> {group_count} групп{stale_marker}")
    return "\n".join(lines)


async def institute_links_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /institute_links from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /institute_links <institute_id>")
        return
    conn = context.bot_data["conn"]
    institute = _resolve_admin_institute(conn, context.args[0])
    if institute is None:
        await update.message.reply_text("Институт с таким id не найден.")
        return
    await update.message.reply_text(format_institute_links(conn, institute.id))

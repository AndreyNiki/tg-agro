import json
import logging

from telegram import Update
from telegram.ext import ContextTypes

from schedule_bot import storage
from schedule_bot.bot import _is_valid_hh_mm, is_admin
from schedule_bot.group_links import refresh_institute_catalog

logger = logging.getLogger(__name__)


def _resolve_admin_institute(conn, institute_id_str: str):
    if not institute_id_str.isdigit():
        return None
    return storage.get_institute(conn, int(institute_id_str))


async def add_institute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("institute_commands: /add_institute from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /add_institute <название> <url_каталога>")
        return
    conn = context.bot_data["conn"]
    config = context.bot_data["config"]
    name = context.args[0]
    catalog_url = context.args[1]
    institute_id = storage.create_institute(
        conn, name=name, catalog_url=catalog_url,
        check_interval_minutes=config.default_check_interval_minutes,
        send_time=config.default_daily_send_time, tz_name=config.default_timezone,
        catalog_refresh_interval_minutes=config.default_catalog_refresh_interval_minutes,
    )
    try:
        refresh_institute_catalog(
            conn, storage.get_institute(conn, institute_id),
            notify_admin=lambda text: None,  # synchronous call site, can't await here — see refresh_institute_links_command for the alert path
        )
        scan_status = "каталог просканирован"
    except Exception as error:
        # Institute creation must succeed even if the catalog page is unreachable right now —
        # a later /refresh_institute_links or the background catalog job can retry the scan.
        logger.warning("institute_commands: initial catalog scan failed for institute_id=%s: %s", institute_id, error)
        scan_status = f"сканирование каталога не удалось: {error}"
    context.bot_data["reschedule_institute_jobs"](institute_id)
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
    alerts = []
    try:
        refresh_institute_catalog(conn, institute, notify_admin=alerts.append)
    except Exception as error:
        await update.message.reply_text(f"Сканирование каталога не удалось: {error}")
        return
    context.bot_data["reschedule_institute_jobs"](institute.id)
    for text in alerts:
        await update.message.reply_text(text)
    await update.message.reply_text(f"Каталог института «{institute.name}» пересканирован.")


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
    links = storage.list_institute_links(conn, institute.id)
    if not links:
        await update.message.reply_text("У этого института пока нет ссылок.")
        return
    lines = []
    for link in links:
        group_count = len(json.loads(link.group_names_json))
        stale_marker = " [stale]" if link.is_stale else ""
        lines.append(f"{link.raw_label!r} -> {group_count} групп{stale_marker}")
    await update.message.reply_text("\n".join(lines))

import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import storage
from schedule_bot.formatting import format_day_schedule
from schedule_bot.pdf_parser import DAY_ORDER
from schedule_bot.screenshot import render_day_image
from schedule_bot.telegram_utils import SEND_PACING_SECONDS, send_with_retry
from schedule_bot.timeutil import today_in_configured_timezone

logger = logging.getLogger(__name__)

DAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]

NOOP_CALLBACK_DATA = "noop"

USER_COMMANDS = [
    ("start", "Подписаться на ежедневную рассылку расписания"),
    ("stop", "Отписаться от рассылки"),
    ("schedule", "Показать расписание на выбранный день"),
    ("help", "Список доступных команд"),
]

ADMIN_COMMANDS = [
    ("set_pdf_source", "Изменить источник PDF с расписанием"),
    ("set_group", "Изменить отслеживаемую группу"),
    ("set_send_time", "Изменить время ежедневной рассылки (HH:MM)"),
    ("set_check_interval", "Изменить интервал проверки обновлений (мин.)"),
    ("refresh", "Принудительно проверить и разослать обновления"),
    ("alert", "Разослать произвольное сообщение всем подписчикам"),
]

STALE_SCHEDULE_WARNING = (
    "⚠️ Последняя синхронизация с сайтом расписания не удалась — данные могли "
    "устареть. Рекомендуем свериться с сайтом.\n\n"
)


def _stale_warning_prefix(conn) -> str:
    return STALE_SCHEDULE_WARNING if storage.get_setting(conn, "last_sync_failed_at") else ""


def _week_dates(monday: date) -> list[date]:
    return [monday + timedelta(days=offset) for offset in range(len(DAY_LABELS))]


def _day_buttons(dates: list[date]) -> list[list[InlineKeyboardButton]]:
    buttons = [
        InlineKeyboardButton(
            f"{DAY_LABELS[day_date.weekday()]} {day_date.strftime('%d.%m')}",
            callback_data=f"day:{day_date.isoformat()}",
        )
        for day_date in dates
    ]
    return [buttons[i : i + 3] for i in range(0, len(buttons), 3)]


def build_day_keyboard(today: date) -> InlineKeyboardMarkup:
    """Two sections so a bare weekday label is never ambiguous about which
    calendar week it falls in: remaining days of the current week (today
    through Saturday — empty on a Sunday, since classes run Mon-Sat), then
    all of next week. Every button also carries its short date."""
    this_monday = today - timedelta(days=today.weekday())
    remaining_this_week = [d for d in _week_dates(this_monday) if d >= today]
    next_week = _week_dates(this_monday + timedelta(days=7))

    rows: list[list[InlineKeyboardButton]] = []
    if remaining_this_week:
        rows.append([InlineKeyboardButton("— Эта неделя —", callback_data=NOOP_CALLBACK_DATA)])
        rows.extend(_day_buttons(remaining_this_week))
    rows.append([InlineKeyboardButton("— Следующая неделя —", callback_data=NOOP_CALLBACK_DATA)])
    rows.extend(_day_buttons(next_week))
    return InlineKeyboardMarkup(rows)


def day_date_from_callback(data: str) -> date:
    if not data.startswith("day:"):
        raise ValueError(f"Unexpected callback data: {data!r}")
    return date.fromisoformat(data.split(":", 1)[1])


async def noop_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    group_name = context.bot_data["group_name"]
    chat_id = update.effective_chat.id
    logger.info("bot: /start from chat_id=%s", chat_id)
    storage.add_subscriber(conn, chat_id, group_name, datetime.now(timezone.utc).isoformat())
    await update.message.reply_text(
        "Вы подписались на ежедневную рассылку расписания. "
        "Команда /schedule покажет расписание на выбранный день."
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    logger.info("bot: /stop from chat_id=%s", chat_id)
    storage.remove_subscriber(conn, chat_id)
    await update.message.reply_text("Подписка отменена.")


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    logger.info("bot: /schedule from chat_id=%s", update.effective_chat.id)
    text = _stale_warning_prefix(conn) + "Выберите день недели:"
    await update.message.reply_text(text, reply_markup=build_day_keyboard(today_in_configured_timezone(conn)))


async def day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    on_date = day_date_from_callback(query.data)
    day_name = DAY_ORDER[on_date.weekday()]
    logger.info("bot: day_callback %s (%s) from chat_id=%s", day_name, on_date, update.effective_chat.id)

    schedule = context.bot_data["active_schedule_getter"]()
    if schedule is None:
        await query.message.reply_text(
            "Расписание ещё не загружено, попробуйте позже."
        )
        return

    week_formula: object = context.bot_data["week_formula"]
    week_type = week_formula.compute(on_date)

    text = format_day_schedule(day_name, on_date, week_type, schedule)
    await query.message.reply_text(text)

    pdf_path = context.bot_data["active_pdf_path_getter"]()
    if pdf_path is None:
        return

    try:
        image_bytes = render_day_image(pdf_path, schedule, day_name)
    except Exception as error:
        logger.warning("bot: failed to render day image for %r: %s", pdf_path, error)
        image_bytes = None

    if image_bytes is not None:
        await query.message.reply_photo(photo=image_bytes)


def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return update.effective_chat.id == context.bot_data["admin_chat_id"]


def _format_command_list(commands: list[tuple[str, str]]) -> str:
    return "\n".join(f"/{name} — {description}" for name, description in commands)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /help from chat_id=%s", update.effective_chat.id)
    lines = ["Доступные команды:", "", _format_command_list(USER_COMMANDS)]
    if is_admin(update, context):
        lines += ["", "Команды администратора:", "", _format_command_list(ADMIN_COMMANDS)]
    await update.message.reply_text("\n".join(lines))


_ALLOWED_PDF_SOURCE_SCHEMES = {"http", "https", ""}  # "" covers a bare local path


def _is_valid_pdf_source(value: str) -> bool:
    return urlparse(value).scheme in _ALLOWED_PDF_SOURCE_SCHEMES


async def set_pdf_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /set_pdf_source from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /set_pdf_source <url_или_путь>")
        return
    value = context.args[0]
    if not _is_valid_pdf_source(value):
        await update.message.reply_text("Неверный источник: разрешены только http(s)-адреса или локальный путь.")
        return
    storage.set_setting(context.bot_data["conn"], "pdf_source", value)
    await update.message.reply_text(f"Источник PDF обновлён: {value}")


async def set_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /set_group from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /set_group <название>")
        return
    conn = context.bot_data["conn"]
    new_group_name = context.args[0]
    # Re-point every existing subscriber at the new group; otherwise they'd
    # be silently dropped from every future broadcast (daily send, change
    # notifications, /alert), since those all filter subscribers by the
    # *current* group_name setting.
    old_group_name = storage.get_setting(conn, "group_name")
    migrated = 0
    if old_group_name and old_group_name != new_group_name:
        migrated = storage.update_subscribers_group(conn, old_group_name, new_group_name)
    storage.set_setting(conn, "group_name", new_group_name)
    await update.message.reply_text(
        f"Группа обновлена: {new_group_name}. Подписчиков перенесено: {migrated}."
    )


_HH_MM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]?\d)$")


def _is_valid_hh_mm(value: str) -> bool:
    return bool(_HH_MM_RE.match(value))


def _is_valid_positive_int(value: str) -> bool:
    try:
        return int(value) > 0
    except ValueError:
        return False


async def set_send_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /set_send_time from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /set_send_time <HH:MM>")
        return
    value = context.args[0]
    if not _is_valid_hh_mm(value):
        await update.message.reply_text("Неверный формат времени, используйте HH:MM.")
        return
    storage.set_setting(context.bot_data["conn"], "daily_send_time", value)
    context.bot_data["reschedule_daily_job"]()
    await update.message.reply_text(f"Время ежедневной рассылки обновлено: {value}")


async def set_check_interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /set_check_interval from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /set_check_interval <минуты>")
        return
    value = context.args[0]
    if not _is_valid_positive_int(value):
        await update.message.reply_text("Неверное значение интервала, укажите положительное целое число минут.")
        return
    storage.set_setting(context.bot_data["conn"], "check_interval_minutes", value)
    context.bot_data["reschedule_hourly_job"]()
    await update.message.reply_text(f"Интервал проверки обновлён: {value} мин.")


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /refresh from chat_id=%s", update.effective_chat.id)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    sync_fn = context.bot_data["sync_fn"]
    try:
        result = await sync_fn()
    except Exception as error:
        logger.warning("bot: /refresh sync failed: %s", error)
        await update.message.reply_text(
            f"Синхронизация не удалась: {error}. Расписание могло устареть."
        )
        return
    if result.status == "no_changes":
        await update.message.reply_text("Изменений нет.")
    elif result.status == "initial":
        await update.message.reply_text("Расписание загружено впервые.")
    else:
        await update.message.reply_text("Расписание обновлено и разослано подписчикам.")


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /alert from chat_id=%s", update.effective_chat.id)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /alert <текст>")
        return
    text = " ".join(context.args)
    conn = context.bot_data["conn"]
    group_name = context.bot_data["group_name"]
    subscribers = storage.list_subscribers(conn, group_name)
    for chat_id in subscribers:
        try:
            await send_with_retry(lambda chat_id=chat_id: context.bot.send_message(chat_id=chat_id, text=text))
        except Exception as error:
            logger.warning("bot: failed to alert chat_id=%s: %s", chat_id, error)
        await asyncio.sleep(SEND_PACING_SECONDS)
    await update.message.reply_text(f"Сообщение разослано {len(subscribers)} подписчикам.")

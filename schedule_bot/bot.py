import asyncio
import logging
import re
from datetime import date, datetime, timedelta, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import elections, storage
from schedule_bot.formatting import format_day_schedule
from schedule_bot.pdf_parser import DAY_ORDER
from schedule_bot.screenshot import render_day_image
from schedule_bot.telegram_utils import SEND_PACING_SECONDS, reply, send_with_retry
from schedule_bot.timeutil import today_in_configured_timezone

logger = logging.getLogger(__name__)

DAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]

NOOP_CALLBACK_DATA = "noop"

USER_COMMANDS = [
    ("start", "Выбрать группу / статус подписки"),
    ("subscribe", "Включить рассылку расписания"),
    ("unsubscribe", "Отключить рассылку (группа остаётся закреплённой)"),
    ("schedule", "Показать расписание на выбранный день"),
    ("classmates", "Список одногруппников"),
    ("starosta", "Статус и управление старостой группы"),
    ("votes", "Открытые голосования группы"),
    ("change_group", "Подать заявку на переход в другую группу"),
    ("help", "Список доступных команд"),
]

ADMIN_COMMANDS = [
    ("add_institute", "Добавить институт: <название> <url_каталога>"),
    ("set_institute_catalog_url", "Изменить url каталога института: <institute_id> <url>"),
    ("set_institute_check_interval", "Изменить интервал проверки института: <institute_id> <минуты>"),
    ("set_institute_send_time", "Изменить время рассылки института: <institute_id> <HH:MM>"),
    ("set_institute_catalog_interval", "Изменить интервал сканирования каталога: <institute_id> <минуты>"),
    ("refresh_institute_links", "Пересканировать каталог института: <institute_id>"),
    ("institute_links", "Диагностика ссылок института: <institute_id>"),
    ("refresh", "Принудительно проверить и разослать обновления по ссылке группы: <group_id>"),
    ("alert", "Разослать объявление всем пользователям бота: <текст>"),
    ("set_starosta", "Назначить старосту группы: <group_id> <chat_id>"),
    ("remove_starosta", "Снять старосту группы: <group_id>"),
    ("close_group", "Закрыть группу на вступление: [group_id] (без аргумента — своя группа)"),
    ("open_group", "Открыть группу для вступления: [group_id] (без аргумента — своя группа)"),
    ("notify", "Оповестить всех закреплённых за группой: [group_id] <текст>"),
    ("requests", "Заявки на вступление/переход: [group_id] (без аргумента — своя группа)"),
]

STALE_SCHEDULE_WARNING = (
    "⚠️ Последняя синхронизация с сайтом расписания не удалась — данные могли "
    "устареть. Рекомендуем свериться с сайтом.\n\n"
)


def _stale_warning_prefix(group) -> str:
    return STALE_SCHEDULE_WARNING if group and group.last_sync_failed_at else ""


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


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    logger.info("bot: /schedule from chat_id=%s", chat_id)
    member = storage.get_member(conn, chat_id)
    if member is None:
        await reply(update, "Сначала выберите группу: /start")
        return
    group = storage.get_group(conn, member.group_id)
    text = _stale_warning_prefix(group) + "Выберите день недели:"
    institute = storage.get_institute(conn, group.institute_id) if group else None
    today = today_in_configured_timezone(institute.timezone if institute else None)
    await reply(update, text, reply_markup=build_day_keyboard(today))


async def day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    on_date = day_date_from_callback(query.data)
    day_name = DAY_ORDER[on_date.weekday()]
    chat_id = update.effective_chat.id
    logger.info("bot: day_callback %s (%s) from chat_id=%s", day_name, on_date, chat_id)

    conn = context.bot_data["conn"]
    member = storage.get_member(conn, chat_id)
    if member is None:
        await query.message.reply_text("Сначала выберите группу: /start")
        return

    schedule = context.bot_data["active_schedule_getter"](member.group_id)
    if schedule is None:
        await query.message.reply_text(
            "Расписание ещё не загружено, попробуйте позже."
        )
        return

    week_formula: object = context.bot_data["week_formula"]
    week_type = week_formula.compute(on_date)

    text = format_day_schedule(day_name, on_date, week_type, schedule)
    await query.message.reply_text(text)

    pdf_path = context.bot_data["active_pdf_path_getter"](member.group_id)
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


_HH_MM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]?\d)$")


def _is_valid_hh_mm(value: str) -> bool:
    return bool(_HH_MM_RE.match(value))


def _is_valid_positive_int(value: str) -> bool:
    try:
        return int(value) > 0
    except ValueError:
        return False


def _resolve_admin_group(conn, group_id_str: str):
    if not group_id_str.isdigit():
        return None
    return storage.get_group(conn, int(group_id_str))


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /refresh from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /refresh <group_id>")
        return
    conn = context.bot_data["conn"]
    group = _resolve_admin_group(conn, context.args[0])
    if group is None:
        await update.message.reply_text("Группа с таким id не найдена.")
        return
    sync_institute_link_fn = context.bot_data["sync_institute_link_fn"]
    try:
        results = await sync_institute_link_fn(group.institute_link_id)
    except Exception as error:
        logger.warning("bot: /refresh sync failed: %s", error)
        await update.message.reply_text(f"Синхронизация не удалась: {error}. Расписание могло устареть.")
        return
    own_result = next((r for r in results if r.group_id == group.id), None)
    if own_result is None:
        await update.message.reply_text("Группа не подписана ни на один чат — синхронизация не выполнялась.")
    elif own_result.status == "no_changes":
        await update.message.reply_text("Изменений нет.")
    elif own_result.status == "initial":
        await update.message.reply_text("Расписание загружено впервые.")
    elif own_result.status == "error":
        await update.message.reply_text("Синхронизация для этой группы не удалась — колонка не найдена или сеть недоступна.")
    else:
        await update.message.reply_text("Расписание обновлено и разослано подписчикам.")


async def _apply_alert(conn, bot_client, text: str) -> str:
    chat_ids = storage.list_all_member_chat_ids(conn)
    for chat_id in chat_ids:
        try:
            await send_with_retry(lambda chat_id=chat_id: bot_client.send_message(chat_id=chat_id, text=text))
        except Exception as error:
            logger.warning("bot: failed to alert chat_id=%s: %s", chat_id, error)
        await asyncio.sleep(SEND_PACING_SECONDS)
    return f"Сообщение разослано {len(chat_ids)} пользователям."


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /alert from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /alert <текст>")
        return
    text = " ".join(context.args)
    await update.message.reply_text(await _apply_alert(context.bot_data["conn"], context.bot, text))


async def _apply_set_starosta(conn, bot_client, group, chat_id_str: str) -> str:
    if not chat_id_str.isdigit():
        return "chat_id должен быть числом."
    new_starosta_chat_id = int(chat_id_str)
    now = datetime.now(timezone.utc).isoformat()
    storage.cancel_open_elections_for_group(conn, group.id, now)
    storage.update_group_starosta(conn, group.id, new_starosta_chat_id)
    storage.set_group_last_starosta_change_at(conn, group.id, now)
    welcome = elections.format_starosta_welcome(group.name, "admin")
    try:
        await send_with_retry(lambda: bot_client.send_message(chat_id=new_starosta_chat_id, text=welcome))
    except Exception as error:
        logger.warning("bot: failed to notify new starosta chat_id=%s: %s", new_starosta_chat_id, error)
    return f"Староста группы «{group.name}» назначен."


def _apply_remove_starosta(conn, group) -> str:
    now = datetime.now(timezone.utc).isoformat()
    storage.cancel_open_elections_for_group(conn, group.id, now)
    storage.update_group_starosta(conn, group.id, None)
    storage.set_group_last_starosta_change_at(conn, group.id, now)
    return f"Староста группы «{group.name}» снят."


async def set_starosta_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /set_starosta from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if len(context.args) < 2:
        await update.message.reply_text("Использование: /set_starosta <group_id> <chat_id>")
        return
    conn = context.bot_data["conn"]
    group = _resolve_admin_group(conn, context.args[0])
    if group is None:
        await update.message.reply_text("Группа с таким id не найдена.")
        return
    text = await _apply_set_starosta(conn, context.bot, group, context.args[1])
    await update.message.reply_text(text)


async def remove_starosta_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /remove_starosta from chat_id=%s args=%s", update.effective_chat.id, context.args)
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /remove_starosta <group_id>")
        return
    conn = context.bot_data["conn"]
    group = _resolve_admin_group(conn, context.args[0])
    if group is None:
        await update.message.reply_text("Группа с таким id не найдена.")
        return
    await update.message.reply_text(_apply_remove_starosta(conn, group))


async def _toggle_group_closed(update: Update, context: ContextTypes.DEFAULT_TYPE, is_closed: bool) -> None:
    conn = context.bot_data["conn"]
    if is_admin(update, context):
        if not context.args:
            command_name = "close_group" if is_closed else "open_group"
            await update.message.reply_text(f"Использование: /{command_name} <group_id>")
            return
        group = _resolve_admin_group(conn, context.args[0])
        if group is None:
            await update.message.reply_text("Группа с таким id не найдена.")
            return
    else:
        group = storage.get_group_where_starosta(conn, update.effective_chat.id)
        if group is None:
            await update.message.reply_text("Вы не староста ни одной группы.")
            return

    storage.update_group_closed(conn, group.id, is_closed)
    state = "закрыта" if is_closed else "открыта"
    await update.message.reply_text(f"Группа «{group.name}» теперь {state}.")


async def close_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /close_group from chat_id=%s args=%s", update.effective_chat.id, context.args)
    await _toggle_group_closed(update, context, True)


async def open_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /open_group from chat_id=%s args=%s", update.effective_chat.id, context.args)
    await _toggle_group_closed(update, context, False)


async def _apply_notify(conn, bot_client, group, text: str) -> str:
    members = storage.list_group_members(conn, group.id, subscribed_only=False)
    for member_chat_id in members:
        try:
            await send_with_retry(
                lambda member_chat_id=member_chat_id: bot_client.send_message(chat_id=member_chat_id, text=text)
            )
        except Exception as error:
            logger.warning("bot: failed to notify chat_id=%s: %s", member_chat_id, error)
        await asyncio.sleep(SEND_PACING_SECONDS)
    return f"Оповещение отправлено {len(members)} участникам группы «{group.name}»."


async def notify_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.info("bot: /notify from chat_id=%s args=%s", update.effective_chat.id, context.args)
    conn = context.bot_data["conn"]

    if is_admin(update, context):
        if len(context.args) < 2:
            await update.message.reply_text("Использование: /notify <group_id> <текст>")
            return
        group = _resolve_admin_group(conn, context.args[0])
        if group is None:
            await update.message.reply_text("Группа с таким id не найдена.")
            return
        text = " ".join(context.args[1:])
    else:
        group = storage.get_group_where_starosta(conn, update.effective_chat.id)
        if group is None:
            await update.message.reply_text("Вы не староста ни одной группы.")
            return
        if not context.args:
            await update.message.reply_text("Использование: /notify <текст>")
            return
        text = " ".join(context.args)

    await update.message.reply_text(await _apply_notify(conn, context.bot, group, text))

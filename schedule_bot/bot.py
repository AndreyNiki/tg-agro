import sys
from datetime import date, datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import storage
from schedule_bot.formatting import format_day_schedule
from schedule_bot.pdf_parser import DAY_ORDER
from schedule_bot.screenshot import render_day_image

DAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]


def build_day_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(label, callback_data=f"day:{index}")
        for index, label in enumerate(DAY_LABELS)
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(rows)


def day_index_from_callback(data: str) -> int:
    if not data.startswith("day:"):
        raise ValueError(f"Unexpected callback data: {data!r}")
    return int(data.split(":", 1)[1])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    group_name = context.bot_data["group_name"]
    chat_id = update.effective_chat.id
    storage.add_subscriber(conn, chat_id, group_name, datetime.now(timezone.utc).isoformat())
    await update.message.reply_text(
        "Подписка оформлена. Каждое утро вы будете получать расписание. "
        "Команда /schedule покажет расписание на выбранный день."
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    storage.remove_subscriber(conn, chat_id)
    await update.message.reply_text("Подписка отменена.")


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Выберите день недели:", reply_markup=build_day_keyboard()
    )


def _next_occurrence_of(day_index: int, today: date) -> date:
    """Nearest date matching the given weekday (0=Понедельник..5=Суббота).

    Python's date.weekday() returns 0=Monday..6=Sunday, which already
    aligns with day_index for Mon-Sat, so this simple formula works.
    """
    from datetime import timedelta

    return today + timedelta(days=(day_index - today.weekday()) % 7)


async def day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    day_index = day_index_from_callback(query.data)
    day_name = DAY_ORDER[day_index]

    schedule = context.bot_data["active_schedule_getter"]()
    if schedule is None:
        await query.message.reply_text(
            "Расписание ещё не загружено, попробуйте позже."
        )
        return

    on_date = _next_occurrence_of(day_index, date.today())
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
        print(f"bot: failed to render day image for {pdf_path!r}: {error}", file=sys.stderr)
        image_bytes = None

    if image_bytes is not None:
        await query.message.reply_photo(photo=image_bytes)


def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return update.effective_chat.id == context.bot_data["admin_chat_id"]


async def set_pdf_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /set_pdf_source <url_или_путь>")
        return
    storage.set_setting(context.bot_data["conn"], "pdf_source", context.args[0])
    await update.message.reply_text(f"Источник PDF обновлён: {context.args[0]}")


async def set_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /set_group <название>")
        return
    storage.set_setting(context.bot_data["conn"], "group_name", context.args[0])
    await update.message.reply_text(f"Группа обновлена: {context.args[0]}")


async def set_send_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /set_send_time <HH:MM>")
        return
    storage.set_setting(context.bot_data["conn"], "daily_send_time", context.args[0])
    context.bot_data["reschedule_daily_job"]()
    await update.message.reply_text(f"Время ежедневной рассылки обновлено: {context.args[0]}")


async def set_check_interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /set_check_interval <минуты>")
        return
    storage.set_setting(context.bot_data["conn"], "check_interval_minutes", context.args[0])
    context.bot_data["reschedule_hourly_job"]()
    await update.message.reply_text(f"Интервал проверки обновлён: {context.args[0]} мин.")


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    sync_fn = context.bot_data["sync_fn"]
    result = await sync_fn()
    if result.status == "no_changes":
        await update.message.reply_text("Изменений нет.")
    elif result.status == "initial":
        await update.message.reply_text("Расписание загружено впервые.")
    else:
        await update.message.reply_text(
            "Расписание обновлено и разослано:\n" + "\n".join(result.changes)
        )


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as error:
            print(f"bot: failed to alert chat_id={chat_id}: {error}", file=sys.stderr)
    await update.message.reply_text(f"Сообщение разослано {len(subscribers)} подписчикам.")

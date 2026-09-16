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


async def day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    day_index = day_index_from_callback(query.data)
    day_name = DAY_ORDER[day_index]

    schedule = context.bot_data["active_schedule_getter"]()
    week_formula: object = context.bot_data["week_formula"]
    week_type = week_formula.compute(date.today())

    text = format_day_schedule(day_name, date.today(), week_type, schedule)
    await query.message.reply_text(text)

    pdf_path = context.bot_data["active_pdf_path_getter"]()
    image_bytes = render_day_image(pdf_path, schedule, day_name)
    if image_bytes is not None:
        await query.message.reply_photo(photo=image_bytes)

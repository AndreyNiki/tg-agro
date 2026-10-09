from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import storage
from schedule_bot.bot import schedule_command
from schedule_bot.change_group_commands import change_group_command
from schedule_bot.classmates import classmates_command
from schedule_bot.group_commands import subscribe_command, unsubscribe_command
from schedule_bot.starosta_commands import votes_command

ROOT_CALLBACK_DATA = "menu:root:_"


def build_root_keyboard(conn, chat_id: int, admin_chat_id: int) -> InlineKeyboardMarkup:
    rows = []
    member = storage.get_member(conn, chat_id)
    if member is not None:
        rows.append([InlineKeyboardButton("📅 Расписание", callback_data="menu:schedule:root")])
        rows.append([InlineKeyboardButton("👥 Группа", callback_data="menu:group:root")])
        rows.append([InlineKeyboardButton("👑 Староста", callback_data="menu:starosta:root")])
    if chat_id == admin_chat_id:
        rows.append([InlineKeyboardButton("⚙️ Админ", callback_data="menu:admin:root")])
    return InlineKeyboardMarkup(rows)


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    keyboard = build_root_keyboard(conn, chat_id, context.bot_data["admin_chat_id"])
    await update.message.reply_text("Главное меню:", reply_markup=keyboard)


def _schedule_section_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📅 Выбрать день", callback_data="menu:schedule:daypicker")],
            [InlineKeyboardButton("🔔 Подписаться/Отписаться", callback_data="menu:schedule:togglesub")],
            [InlineKeyboardButton("◀️ Назад", callback_data=ROOT_CALLBACK_DATA)],
        ]
    )


def _group_section_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📋 Одногруппники", callback_data="menu:group:classmates")],
            [InlineKeyboardButton("🔄 Сменить группу", callback_data="menu:group:changegroup")],
            [InlineKeyboardButton("🗳 Открытые голосования", callback_data="menu:group:votes")],
            [InlineKeyboardButton("◀️ Назад", callback_data=ROOT_CALLBACK_DATA)],
        ]
    )


async def _handle_schedule_section(update, context, conn, chat_id: int, action: str) -> None:
    query = update.callback_query
    if action == "root":
        await query.edit_message_text("Расписание:", reply_markup=_schedule_section_keyboard())
        return
    if action == "daypicker":
        await schedule_command(update, context)
        return
    if action == "togglesub":
        member = storage.get_member(conn, chat_id)
        if member is None:
            await query.message.reply_text("Сначала выберите группу: /start")
            return
        if member.subscribed:
            await unsubscribe_command(update, context)
        else:
            await subscribe_command(update, context)
        return


async def _handle_group_section(update, context, conn, chat_id: int, action: str) -> None:
    query = update.callback_query
    if action == "root":
        await query.edit_message_text("Группа:", reply_markup=_group_section_keyboard())
        return
    if action == "classmates":
        await classmates_command(update, context)
        return
    if action == "changegroup":
        await change_group_command(update, context)
        return
    if action == "votes":
        await votes_command(update, context)
        return


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    admin_chat_id = context.bot_data["admin_chat_id"]
    _, section, action = query.data.split(":", 2)

    if section == "root":
        await query.edit_message_text("Главное меню:", reply_markup=build_root_keyboard(conn, chat_id, admin_chat_id))
        return
    if section == "schedule":
        await _handle_schedule_section(update, context, conn, chat_id, action)
        return
    if section == "group":
        await _handle_group_section(update, context, conn, chat_id, action)
        return

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import storage

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

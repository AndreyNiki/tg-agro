import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import storage

logger = logging.getLogger(__name__)

GROUPS_PER_PAGE = 8

SELECT_PREFIX = "grp:select:"
PAGE_PREFIX = "grp:page:"
CREATE_CALLBACK_DATA = "grp:create"

SELECT_CALLBACK_PATTERN = r"^grp:select:\d+$"
PAGE_CALLBACK_PATTERN = r"^grp:page:\d+$"


def build_group_picker_keyboard(conn, page: int = 0) -> InlineKeyboardMarkup:
    offset = page * GROUPS_PER_PAGE
    groups = storage.list_groups(conn, limit=GROUPS_PER_PAGE, offset=offset)
    total = storage.count_groups(conn)

    rows = [[InlineKeyboardButton(group.name, callback_data=f"{SELECT_PREFIX}{group.id}")] for group in groups]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"{PAGE_PREFIX}{page - 1}"))
    if offset + GROUPS_PER_PAGE < total:
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"{PAGE_PREFIX}{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    rows.append([InlineKeyboardButton("➕ Создать группу", callback_data=CREATE_CALLBACK_DATA)])
    return InlineKeyboardMarkup(rows)


async def group_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.removeprefix(PAGE_PREFIX))
    conn = context.bot_data["conn"]
    await query.message.edit_reply_markup(reply_markup=build_group_picker_keyboard(conn, page))


async def group_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    group_id = int(query.data.removeprefix(SELECT_PREFIX))
    conn = context.bot_data["conn"]
    group = storage.get_group(conn, group_id)
    if group is None:
        await query.message.reply_text("Эта группа больше не существует.")
        return

    chat_id = update.effective_chat.id
    storage.upsert_member(conn, chat_id, group_id, joined_at=datetime.now(timezone.utc).isoformat(), subscribed=True)
    storage.update_member_profile(
        conn, chat_id,
        telegram_username=update.effective_user.username,
        display_name=update.effective_user.full_name,
    )
    await query.message.reply_text(
        f"Вы подписались на рассылку расписания группы «{group.name}». "
        "Команда /schedule покажет расписание на выбранный день."
    )

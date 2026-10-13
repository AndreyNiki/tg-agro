import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import elections, storage
from schedule_bot.telegram_utils import safe_edit_reply_markup

logger = logging.getLogger(__name__)

CANDIDATES_PER_PAGE = 8

_SELECT_PREFIX = "starcand:select:"
_PAGE_PREFIX = "starcand:page:"
_CONFIRM_PREFIX = "starcand:confirm:"
_ABORT_CALLBACK_DATA = "starcand:abort"


def build_candidate_keyboard(conn, group_id: int, page: int = 0) -> InlineKeyboardMarkup:
    chat_ids = sorted(storage.list_group_members(conn, group_id, subscribed_only=False))
    offset = page * CANDIDATES_PER_PAGE
    page_ids = chat_ids[offset : offset + CANDIDATES_PER_PAGE]

    rows = []
    for candidate_chat_id in page_ids:
        candidate = storage.get_member(conn, candidate_chat_id)
        label = elections.format_candidate_label(candidate)
        rows.append([InlineKeyboardButton(label, callback_data=f"{_SELECT_PREFIX}{candidate_chat_id}")])

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"{_PAGE_PREFIX}{page - 1}"))
    if offset + CANDIDATES_PER_PAGE < len(chat_ids):
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"{_PAGE_PREFIX}{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    return InlineKeyboardMarkup(rows)


async def page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.removeprefix(_PAGE_PREFIX))
    conn = context.bot_data["conn"]
    member = storage.get_member(conn, update.effective_chat.id)
    if member is None:
        await query.message.reply_text("Вы больше не в какой-либо группе — используйте /start.")
        return
    await safe_edit_reply_markup(query, reply_markup=build_candidate_keyboard(conn, member.group_id, page))


async def select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    candidate_chat_id = int(query.data.removeprefix(_SELECT_PREFIX))
    conn = context.bot_data["conn"]
    candidate = storage.get_member(conn, candidate_chat_id)
    label = elections.format_candidate_label(candidate)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"{_CONFIRM_PREFIX}{candidate_chat_id}"),
                InlineKeyboardButton("✖️ Отмена", callback_data=_ABORT_CALLBACK_DATA),
            ]
        ]
    )
    await query.message.reply_text(
        f"Запустить голосование за {label} в качестве старосты? Это разошлётся всей группе.",
        reply_markup=keyboard,
    )


async def abort_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Отменено.")


async def confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from schedule_bot.starosta_commands import _broadcast_election

    query = update.callback_query
    await query.answer()
    candidate_chat_id = int(query.data.removeprefix(_CONFIRM_PREFIX))
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await query.message.reply_text("Сначала выберите группу: /start")
        return
    group = storage.get_group(conn, member.group_id)

    if group.starosta_chat_id is None:
        await query.message.reply_text("В группе больше нет старосты — используйте /starosta.")
        return

    remaining = elections.cooldown_remaining(group.last_starosta_change_at, datetime.now(timezone.utc))
    if remaining is not None:
        next_date = (datetime.now(timezone.utc) + remaining).date().isoformat()
        await query.message.reply_text(f"Старосту можно сменить не раньше {next_date}.")
        return

    if storage.get_open_election(conn, group.id) is not None:
        await query.message.reply_text("Голосование уже идёт.")
        return

    member_count = len(storage.list_group_members(conn, group.id, subscribed_only=False))
    if not elections.has_enough_members(member_count):
        await query.message.reply_text(
            "В группе недостаточно участников для голосования, пригласите несколько человек и попробуйте снова."
        )
        return

    if candidate_chat_id not in storage.list_group_members(conn, group.id, subscribed_only=False):
        await query.message.reply_text("Этот участник больше не в вашей группе.")
        return

    election_id = storage.create_election(
        conn, group.id, "change", chat_id, candidate_chat_id, datetime.now(timezone.utc).isoformat()
    )
    await _broadcast_election(context.bot, conn, group, election_id, candidate_chat_id, "change")
    await query.message.reply_text("Голосование запущено.")

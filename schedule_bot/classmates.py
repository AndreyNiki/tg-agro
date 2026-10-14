import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from schedule_bot import storage
from schedule_bot.bot import sync_visible_commands
from schedule_bot.elections import format_candidate_label
from schedule_bot.telegram_utils import chunk_text_by_lines, reply, send_with_retry

logger = logging.getLogger(__name__)

# How many "❌ Исключить" buttons to show per page — keeps the keyboard
# well under Telegram's size limit for any group, however large.
EXCLUDE_PAGE_SIZE = 20
_EXCLUDE_PAGE_PREFIX = "exclude:page:"
EXCLUDE_PAGE_CALLBACK_PATTERN = r"^exclude:page:\d+:\d+$"


def format_member_line(member, is_starosta: bool) -> str:
    label = format_candidate_label(member)
    return f"👑 {label}" if is_starosta else label


def _build_exclude_keyboard(excludable_members: list, group_id: int, *, after_chat_id: int = 0) -> InlineKeyboardMarkup | None:
    """Paginates by chat_id (a stable, callback_data-safe cursor) rather
    than by numeric offset, so excluding someone on an earlier page can
    never shift a later page's window and silently skip a member — the
    same bug class the request-listing pagination had with a plain
    offset."""
    page = [m for m in excludable_members if m.chat_id > after_chat_id][:EXCLUDE_PAGE_SIZE]
    if not page:
        return None
    rows = [
        [InlineKeyboardButton(f"❌ Исключить {format_candidate_label(m)}", callback_data=f"exclude:select:{m.chat_id}")]
        for m in page
    ]
    last_chat_id = page[-1].chat_id
    if any(m.chat_id > last_chat_id for m in excludable_members):
        rows.append(
            [InlineKeyboardButton("➡️ Показать ещё", callback_data=f"{_EXCLUDE_PAGE_PREFIX}{group_id}:{last_chat_id}")]
        )
    return InlineKeyboardMarkup(rows)


async def classmates_command(update, context) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await reply(update, "Сначала выберите группу: /start")
        return

    group = storage.get_group(conn, member.group_id)
    chat_ids = storage.list_group_members(conn, group.id, subscribed_only=False)
    members = [storage.get_member(conn, cid) for cid in chat_ids]
    lines = sorted(format_member_line(m, m.chat_id == group.starosta_chat_id) for m in members)

    text = "\n".join(lines)
    if group.starosta_chat_id is not None:
        text += "\n\n👑 — староста группы"

    for chunk in chunk_text_by_lines(text):
        await reply(update, chunk)

    admin_chat_id = context.bot_data.get("admin_chat_id")
    can_exclude = group.starosta_chat_id == chat_id or chat_id == admin_chat_id
    if can_exclude:
        excludable = sorted(
            (m for m in members if m.chat_id != chat_id and m.chat_id != admin_chat_id), key=lambda m: m.chat_id
        )
        keyboard = _build_exclude_keyboard(excludable, group.id)
        if keyboard is not None:
            await reply(update, "Исключить участника:", reply_markup=keyboard)


async def exclude_page_callback(update, context) -> None:
    query = update.callback_query
    await query.answer()
    _, _, group_id_str, after_chat_id_str = query.data.split(":")
    group_id, after_chat_id = int(group_id_str), int(after_chat_id_str)
    conn = context.bot_data["conn"]
    caller_chat_id = update.effective_chat.id
    group = storage.get_group(conn, group_id)
    if group is None:
        await query.message.reply_text("Эта группа больше не существует.")
        return
    admin_chat_id = context.bot_data.get("admin_chat_id")
    if not (group.starosta_chat_id == caller_chat_id or caller_chat_id == admin_chat_id):
        await query.message.reply_text("Недостаточно прав.")
        return
    chat_ids = storage.list_group_members(conn, group.id, subscribed_only=False)
    members = [storage.get_member(conn, cid) for cid in chat_ids]
    excludable = sorted(
        (m for m in members if m.chat_id != caller_chat_id and m.chat_id != admin_chat_id), key=lambda m: m.chat_id
    )
    keyboard = _build_exclude_keyboard(excludable, group_id, after_chat_id=after_chat_id)
    if keyboard is None:
        await query.message.reply_text("Больше участников нет.")
        return
    await query.message.reply_text("Исключить участника:", reply_markup=keyboard)


async def exclude_select_callback(update, context) -> None:
    query = update.callback_query
    await query.answer()
    target_chat_id = int(query.data.split(":")[2])
    caller_chat_id = update.effective_chat.id
    if target_chat_id == caller_chat_id:
        await query.message.reply_text("Нельзя исключить самого себя.")
        return
    if target_chat_id == context.bot_data.get("admin_chat_id"):
        await query.message.reply_text("Нельзя исключить администратора.")
        return
    conn = context.bot_data["conn"]
    target = storage.get_member(conn, target_chat_id)
    if target is None:
        await query.message.reply_text("Этот участник уже не в группе.")
        return
    group = storage.get_group(conn, target.group_id)
    caller_is_admin = caller_chat_id == context.bot_data.get("admin_chat_id")
    caller_is_starosta = group.starosta_chat_id == caller_chat_id
    if not (caller_is_admin or caller_is_starosta):
        await query.message.reply_text("Недостаточно прав.")
        return
    label = format_candidate_label(target)
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"exclude:confirm:{target_chat_id}"),
                InlineKeyboardButton("✖️ Отмена", callback_data="exclude:abort"),
            ]
        ]
    )
    await query.message.reply_text(f"Исключить {label} из группы?", reply_markup=keyboard)


async def exclude_abort_callback(update, context) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Отменено.")


async def exclude_confirm_callback(update, context) -> None:
    query = update.callback_query
    await query.answer()
    target_chat_id = int(query.data.split(":")[2])
    caller_chat_id = update.effective_chat.id
    if target_chat_id == caller_chat_id:
        await query.message.reply_text("Нельзя исключить самого себя.")
        return
    if target_chat_id == context.bot_data.get("admin_chat_id"):
        await query.message.reply_text("Нельзя исключить администратора.")
        return

    conn = context.bot_data["conn"]
    target = storage.get_member(conn, target_chat_id)
    if target is None:
        await query.message.reply_text("Этот участник уже не в группе.")
        return
    group = storage.get_group(conn, target.group_id)
    caller_is_admin = caller_chat_id == context.bot_data.get("admin_chat_id")
    caller_is_starosta = group.starosta_chat_id == caller_chat_id
    if not (caller_is_admin or caller_is_starosta):
        await query.message.reply_text("Недостаточно прав.")
        return

    now = datetime.now(timezone.utc).isoformat()
    storage.create_group_exclusion(conn, group.id, target_chat_id, now, caller_chat_id)
    storage.remove_member(conn, target_chat_id)
    await sync_visible_commands(context.bot, conn, target_chat_id)
    if group.starosta_chat_id == target_chat_id:
        storage.cancel_open_elections_for_group(conn, group.id, now)
        storage.update_group_starosta(conn, group.id, None)
        storage.set_group_last_starosta_change_at(conn, group.id, now)
    try:
        await send_with_retry(
            lambda: context.bot.send_message(
                chat_id=target_chat_id, text=f"Вы были исключены из группы «{group.name}»."
            )
        )
    except Exception as error:
        logger.warning("classmates: failed to notify excluded chat_id=%s: %s", target_chat_id, error)
    await query.message.reply_text("Участник исключён.")

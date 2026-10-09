import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from schedule_bot import storage
from schedule_bot.elections import format_candidate_label
from schedule_bot.telegram_utils import send_with_retry

logger = logging.getLogger(__name__)


def format_member_line(member, is_starosta: bool) -> str:
    label = format_candidate_label(member)
    return f"👑 {label}" if is_starosta else label


async def classmates_command(update, context) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await update.message.reply_text("Сначала выберите группу: /start")
        return

    group = storage.get_group(conn, member.group_id)
    chat_ids = storage.list_group_members(conn, group.id, subscribed_only=False)
    members = [storage.get_member(conn, cid) for cid in chat_ids]
    lines = sorted(format_member_line(m, m.chat_id == group.starosta_chat_id) for m in members)

    text = "\n".join(lines)
    if group.starosta_chat_id is not None:
        text += "\n\n👑 — староста группы"

    admin_chat_id = context.bot_data.get("admin_chat_id")
    can_exclude = group.starosta_chat_id == chat_id or chat_id == admin_chat_id
    if can_exclude:
        rows = []
        for m in members:
            if m.chat_id == chat_id:
                continue
            label = format_member_line(m, m.chat_id == group.starosta_chat_id)
            rows.append([InlineKeyboardButton(f"❌ Исключить {label}", callback_data=f"exclude:select:{m.chat_id}")])
        if rows:
            await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(rows))
            return

    await update.message.reply_text(text)


async def exclude_select_callback(update, context) -> None:
    query = update.callback_query
    await query.answer()
    target_chat_id = int(query.data.split(":")[2])
    caller_chat_id = update.effective_chat.id
    if target_chat_id == caller_chat_id:
        await query.message.reply_text("Нельзя исключить самого себя.")
        return
    conn = context.bot_data["conn"]
    target = storage.get_member(conn, target_chat_id)
    if target is None:
        await query.message.reply_text("Этот участник уже не в группе.")
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
    try:
        await send_with_retry(
            lambda: context.bot.send_message(
                chat_id=target_chat_id, text=f"Вы были исключены из группы «{group.name}»."
            )
        )
    except Exception as error:
        logger.warning("classmates: failed to notify excluded chat_id=%s: %s", target_chat_id, error)
    await query.message.reply_text("Участник исключён.")

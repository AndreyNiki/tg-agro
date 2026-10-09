import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import storage
from schedule_bot.group_commands import GROUPS_PER_PAGE
from schedule_bot.telegram_utils import send_with_retry

logger = logging.getLogger(__name__)

_SELECT_PREFIX = "chggrp:select:"
_PAGE_PREFIX = "chggrp:page:"
_CONFIRM_PREFIX = "chgconfirm:"
_ABORT_CALLBACK_DATA = "chgabort"


def build_change_group_keyboard(conn, exclude_group_id: int, page: int = 0) -> InlineKeyboardMarkup:
    candidates = [g for g in storage.list_all_groups(conn) if g.id != exclude_group_id]
    offset = page * GROUPS_PER_PAGE
    page_groups = candidates[offset : offset + GROUPS_PER_PAGE]

    rows = [[InlineKeyboardButton(g.name, callback_data=f"{_SELECT_PREFIX}{g.id}")] for g in page_groups]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("« Назад", callback_data=f"{_PAGE_PREFIX}{page - 1}"))
    if offset + GROUPS_PER_PAGE < len(candidates):
        nav_row.append(InlineKeyboardButton("Вперёд »", callback_data=f"{_PAGE_PREFIX}{page + 1}"))
    if nav_row:
        rows.append(nav_row)

    return InlineKeyboardMarkup(rows)


async def change_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await update.message.reply_text("Сначала выберите группу: /start")
        return
    if storage.get_pending_request_for_chat(conn, chat_id) is not None:
        await update.message.reply_text("У вас уже есть заявка на рассмотрении.")
        return
    keyboard = build_change_group_keyboard(conn, member.group_id)
    await update.message.reply_text("Выберите новую группу:", reply_markup=keyboard)


async def change_group_page_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    page = int(query.data.removeprefix(_PAGE_PREFIX))
    conn = context.bot_data["conn"]
    member = storage.get_member(conn, update.effective_chat.id)
    await query.message.edit_reply_markup(reply_markup=build_change_group_keyboard(conn, member.group_id, page))


async def change_group_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    target_group_id = int(query.data.removeprefix(_SELECT_PREFIX))
    conn = context.bot_data["conn"]
    target_group = storage.get_group(conn, target_group_id)
    if target_group is None:
        await query.message.reply_text("Эта группа больше не существует.")
        return
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"{_CONFIRM_PREFIX}{target_group_id}"),
                InlineKeyboardButton("✖️ Отмена", callback_data=_ABORT_CALLBACK_DATA),
            ]
        ]
    )
    await query.message.reply_text(
        f"Подать заявку на переход в группу «{target_group.name}»?", reply_markup=keyboard
    )


async def change_group_abort_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.message.reply_text("Отменено.")


async def _notify_approvers(bot, conn, target_group, request_id: int, requester_chat_id: int, admin_chat_id: int) -> None:
    requester = storage.get_member(conn, requester_chat_id)
    label = requester.telegram_username or requester.display_name or "Неизвестный"
    text = f"Заявка на переход в группу «{target_group.name}» от {label}. Одобрить / Отклонить"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"chgreq:approve:{request_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"chgreq:reject:{request_id}"),
            ]
        ]
    )
    recipients = {admin_chat_id}
    if target_group.starosta_chat_id is not None:
        recipients.add(target_group.starosta_chat_id)
    for recipient_chat_id in recipients:
        try:
            await send_with_retry(
                lambda recipient_chat_id=recipient_chat_id: bot.send_message(
                    chat_id=recipient_chat_id, text=text, reply_markup=keyboard
                )
            )
        except Exception as error:
            logger.warning("change_group_commands: failed to notify chat_id=%s: %s", recipient_chat_id, error)


async def change_group_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    target_group_id = int(query.data.removeprefix(_CONFIRM_PREFIX))
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await query.message.reply_text("Сначала выберите группу: /start")
        return
    # Re-check fresh, immediately before creating the request: this guards against the
    # race between opening the picker (change_group_command's earlier check) and the
    # user confirming here, in case a request was created in between.
    if storage.get_pending_request_for_chat(conn, chat_id) is not None:
        await query.message.reply_text("У вас уже есть заявка на рассмотрении.")
        return
    target_group = storage.get_group(conn, target_group_id)
    if target_group is None:
        await query.message.reply_text("Эта группа больше не существует.")
        return

    request_id = storage.create_group_change_request(
        conn, chat_id, member.group_id, target_group_id, datetime.now(timezone.utc).isoformat()
    )
    await _notify_approvers(context.bot, conn, target_group, request_id, chat_id, context.bot_data["admin_chat_id"])
    await query.message.reply_text("Заявка отправлена, ожидайте решения.")


def _can_decide(caller_chat_id: int, target_group, admin_chat_id: int) -> bool:
    is_admin_caller = caller_chat_id == admin_chat_id
    is_target_starosta = target_group is not None and target_group.starosta_chat_id == caller_chat_id
    return is_admin_caller or is_target_starosta


async def change_request_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    request_id = int(query.data.split(":")[2])
    conn = context.bot_data["conn"]
    caller_chat_id = update.effective_chat.id
    request = storage.get_group_change_request(conn, request_id)
    if request is None or request.status != "pending":
        await query.message.reply_text("Эта заявка уже обработана.")
        return
    target_group = storage.get_group(conn, request.to_group_id)
    if not _can_decide(caller_chat_id, target_group, context.bot_data["admin_chat_id"]):
        await query.message.reply_text("Недостаточно прав для решения по этой заявке.")
        return

    now = datetime.now(timezone.utc).isoformat()
    storage.decide_group_change_request(conn, request_id, "approved", now)
    storage.upsert_member(conn, request.chat_id, request.to_group_id, joined_at=now, subscribed=True)
    try:
        await send_with_retry(
            lambda: context.bot.send_message(
                chat_id=request.chat_id,
                text=f"Ваша заявка на переход в группу «{target_group.name}» одобрена.",
            )
        )
    except Exception as error:
        logger.warning("change_group_commands: failed to notify requester chat_id=%s: %s", request.chat_id, error)
    await query.message.reply_text("Заявка одобрена.")


async def change_request_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    request_id = int(query.data.split(":")[2])
    conn = context.bot_data["conn"]
    caller_chat_id = update.effective_chat.id
    request = storage.get_group_change_request(conn, request_id)
    if request is None or request.status != "pending":
        await query.message.reply_text("Эта заявка уже обработана.")
        return
    target_group = storage.get_group(conn, request.to_group_id)
    if not _can_decide(caller_chat_id, target_group, context.bot_data["admin_chat_id"]):
        await query.message.reply_text("Недостаточно прав для решения по этой заявке.")
        return

    now = datetime.now(timezone.utc).isoformat()
    storage.decide_group_change_request(conn, request_id, "rejected", now)
    try:
        await send_with_retry(
            lambda: context.bot.send_message(
                chat_id=request.chat_id,
                text=f"Ваша заявка на переход в группу «{target_group.name}» отклонена.",
            )
        )
    except Exception as error:
        logger.warning("change_group_commands: failed to notify requester chat_id=%s: %s", request.chat_id, error)
    await query.message.reply_text("Заявка отклонена.")

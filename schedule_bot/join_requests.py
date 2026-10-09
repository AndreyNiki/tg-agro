import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from schedule_bot import storage
from schedule_bot.telegram_utils import send_with_retry

logger = logging.getLogger(__name__)


def requires_approval(conn, group, chat_id: int) -> bool:
    return bool(group.is_closed) or storage.has_group_exclusion(conn, group.id, chat_id)


async def notify_approvers(bot, conn, target_group, request_id: int, requester_chat_id: int, admin_chat_id: int) -> None:
    requester = storage.get_member(conn, requester_chat_id)
    label = "Неизвестный"
    if requester is not None:
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
            logger.warning("join_requests: failed to notify chat_id=%s: %s", recipient_chat_id, error)


async def create_join_request_and_notify(
    bot, conn, chat_id: int, from_group_id: int | None, target_group, admin_chat_id: int
) -> int:
    request_id = storage.create_group_change_request(
        conn, chat_id, from_group_id, target_group.id, datetime.now(timezone.utc).isoformat()
    )
    await notify_approvers(bot, conn, target_group, request_id, chat_id, admin_chat_id)
    return request_id

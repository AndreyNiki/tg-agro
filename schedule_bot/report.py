import logging
from datetime import datetime, timezone

from schedule_bot import storage
from schedule_bot.change_group_commands import build_info_decision_keyboard
from schedule_bot.elections import format_candidate_label
from schedule_bot.telegram_utils import send_with_retry

logger = logging.getLogger(__name__)


def format_report_message(reporter, group, chat_id: int, text: str) -> str:
    label = format_candidate_label(reporter) if reporter is not None else "Неизвестный"
    group_name = group.name if group is not None else "—"
    return f"🆘 Жалоба от {label} (chat_id={chat_id}, группа: {group_name}):\n\n{text}"


async def _apply_report(conn, bot_client, admin_chat_id: int, chat_id: int, text: str) -> str:
    reporter = storage.get_member(conn, chat_id)
    group = storage.get_group(conn, reporter.group_id) if reporter is not None else None
    message = format_report_message(reporter, group, chat_id, text)
    request_id = storage.create_group_change_request(
        conn, chat_id, None, None, datetime.now(timezone.utc).isoformat(), kind="report", request_text=text
    )
    keyboard = build_info_decision_keyboard(request_id)
    try:
        await send_with_retry(
            lambda: bot_client.send_message(chat_id=admin_chat_id, text=message, reply_markup=keyboard)
        )
    except Exception as error:
        logger.warning("report: failed to forward to admin: %s", error)
    return "Жалоба отправлена администратору."


async def report_command(update, context) -> None:
    if not context.args:
        await update.message.reply_text("Использование: /report <текст>")
        return
    text = " ".join(context.args)
    conn = context.bot_data["conn"]
    result = await _apply_report(
        conn, context.bot, context.bot_data["admin_chat_id"], update.effective_chat.id, text
    )
    await update.message.reply_text(result)

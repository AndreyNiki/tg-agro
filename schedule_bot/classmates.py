from schedule_bot import storage
from schedule_bot.elections import format_candidate_label


def format_member_line(member, is_starosta: bool) -> str:
    label = format_candidate_label(member)
    return f"👑 {label}" if is_starosta else label


async def classmates_command(update, context) -> None:
    conn = context.bot_data["conn"]
    member = storage.get_member(conn, update.effective_chat.id)
    if member is None:
        await update.message.reply_text("Сначала выберите группу: /start")
        return

    group = storage.get_group(conn, member.group_id)
    chat_ids = storage.list_group_members(conn, group.id, subscribed_only=False)
    members = [storage.get_member(conn, chat_id) for chat_id in chat_ids]
    lines = sorted(format_member_line(m, m.chat_id == group.starosta_chat_id) for m in members)

    text = "\n".join(lines)
    if group.starosta_chat_id is not None:
        text += "\n\n👑 — староста группы"
    await update.message.reply_text(text)

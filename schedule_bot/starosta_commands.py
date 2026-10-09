import asyncio
import logging
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import elections, storage
from schedule_bot.telegram_utils import SEND_PACING_SECONDS, reply, send_with_retry

logger = logging.getLogger(__name__)


async def starosta_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await reply(update, "Сначала выберите группу: /start")
        return
    group = storage.get_group(conn, member.group_id)

    if storage.get_open_election(conn, group.id) is not None:
        await reply(update, "По вашей группе уже идёт голосование. Посмотреть его можно командой /votes.")
        return

    if group.starosta_chat_id is None:
        member_count = len(storage.list_group_members(conn, group.id, subscribed_only=False))
        if not elections.has_enough_members(member_count):
            await reply(update,
                "В группе недостаточно участников для голосования, пригласите несколько человек и попробуйте снова."
            )
            return
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🙋 Я староста", callback_data="starosta:nominate_self")]]
        )
        await reply(update, "В группе ещё нет старосты.", reply_markup=keyboard)
        return

    is_current_starosta = group.starosta_chat_id == chat_id

    remaining = elections.cooldown_remaining(group.last_starosta_change_at, datetime.now(timezone.utc))
    if remaining is not None:
        next_date = (datetime.now(timezone.utc) + remaining).date().isoformat()
        await reply(update, f"Старосту можно сменить не раньше {next_date}.")
        return

    member_count = len(storage.list_group_members(conn, group.id, subscribed_only=False))
    if not elections.has_enough_members(member_count):
        await reply(update,
            "В группе недостаточно участников для голосования, пригласите несколько человек и попробуйте снова."
        )
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔁 Староста изменился", callback_data="starosta:change")]]
    )
    status_line = f"Вы староста группы «{group.name}»." if is_current_starosta else "В группе есть староста."
    await reply(update, status_line, reply_markup=keyboard)


async def _broadcast_election(bot, conn, group, election_id: int, candidate_chat_id: int, kind: str) -> None:
    candidate = storage.get_member(conn, candidate_chat_id)
    label = elections.format_candidate_label(candidate)
    verb = "предлагает себя старостой" if kind == "initial" else "выдвинут кандидатом в старосты"
    text = f"{label} {verb}. Одобрить / Отклонить"
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Одобрить", callback_data=f"election:approve:{election_id}"),
                InlineKeyboardButton("❌ Отклонить", callback_data=f"election:reject:{election_id}"),
            ]
        ]
    )
    for member_chat_id in storage.list_group_members(conn, group.id, subscribed_only=False):
        try:
            await send_with_retry(
                lambda member_chat_id=member_chat_id: bot.send_message(
                    chat_id=member_chat_id, text=text, reply_markup=keyboard
                )
            )
        except Exception as error:
            logger.warning("starosta_commands: failed to notify chat_id=%s: %s", member_chat_id, error)
        await asyncio.sleep(SEND_PACING_SECONDS)


async def nominate_self_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    member = storage.get_member(conn, chat_id)
    if member is None:
        await query.message.reply_text("Сначала выберите группу: /start")
        return
    group = storage.get_group(conn, member.group_id)

    if group.starosta_chat_id is not None:
        await query.message.reply_text("В группе уже есть староста.")
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

    election_id = storage.create_election(
        conn, group.id, "initial", chat_id, chat_id, datetime.now(timezone.utc).isoformat()
    )
    await _broadcast_election(context.bot, conn, group, election_id, chat_id, "initial")
    await query.message.reply_text("Голосование запущено.")


async def starosta_change_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Candidate picker + confirmation implemented in Task 6.
    query = update.callback_query
    await query.answer()
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
    if storage.get_open_election(conn, group.id) is not None:
        await query.message.reply_text("Голосование уже идёт.")
        return
    remaining = elections.cooldown_remaining(group.last_starosta_change_at, datetime.now(timezone.utc))
    if remaining is not None:
        await query.message.reply_text("Пока нельзя — кулдаун ещё не закончился.")
        return
    member_count = len(storage.list_group_members(conn, group.id, subscribed_only=False))
    if not elections.has_enough_members(member_count):
        await query.message.reply_text(
            "В группе недостаточно участников для голосования, пригласите несколько человек и попробуйте снова."
        )
        return
    from schedule_bot.starosta_candidates import build_candidate_keyboard

    await query.message.reply_text(
        "Выберите нового старосту:", reply_markup=build_candidate_keyboard(conn, group.id)
    )


async def _apply_election_result(bot, conn, election: "storage.ElectionRow") -> None:
    now = datetime.now(timezone.utc).isoformat()
    storage.close_election(conn, election.id, "approved", now)
    group = storage.get_group(conn, election.group_id)
    old_starosta_chat_id = group.starosta_chat_id
    storage.update_group_starosta(conn, group.id, election.candidate_chat_id)
    storage.set_group_last_starosta_change_at(conn, group.id, now)

    welcome = elections.format_starosta_welcome(group.name, "election")
    try:
        await send_with_retry(lambda: bot.send_message(chat_id=election.candidate_chat_id, text=welcome))
    except Exception as error:
        logger.warning("starosta_commands: failed to send welcome to chat_id=%s: %s", election.candidate_chat_id, error)

    if election.kind == "change" and old_starosta_chat_id and old_starosta_chat_id != election.candidate_chat_id:
        try:
            await send_with_retry(
                lambda: bot.send_message(
                    chat_id=old_starosta_chat_id, text=f"Старосту группы «{group.name}» сменили."
                )
            )
        except Exception as error:
            logger.warning(
                "starosta_commands: failed to notify old starosta chat_id=%s: %s", old_starosta_chat_id, error
            )


async def election_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    election_id = int(query.data.split(":")[2])
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id

    election = storage.get_election(conn, election_id)
    if election is None or election.status != "open":
        await query.message.reply_text("Это голосование уже завершено.")
        return

    member = storage.get_member(conn, chat_id)
    if member is None or member.group_id != election.group_id:
        await query.message.reply_text("Вы больше не в этой группе.")
        return

    if chat_id == election.candidate_chat_id:
        await query.message.reply_text("Вы не можете голосовать за себя.")
        return

    storage.add_election_approval(conn, election_id, chat_id, datetime.now(timezone.utc).isoformat())
    count = storage.count_election_approvals(conn, election_id)
    if count >= 2:
        await _apply_election_result(context.bot, conn, election)
    else:
        await query.message.reply_text(f"Голос засчитан ({count}/2).")


async def election_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer("Отклонения не учитываются, решает количество одобрений.")


async def votes_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    member = storage.get_member(conn, update.effective_chat.id)
    if member is None:
        await reply(update, "Сначала выберите группу: /start")
        return

    open_elections = storage.list_open_elections(conn, member.group_id)
    if not open_elections:
        await reply(update, "Нет открытых голосований.")
        return

    for election in open_elections:
        candidate = storage.get_member(conn, election.candidate_chat_id)
        count = storage.count_election_approvals(conn, election.id)
        kind_label = "Первое назначение старосты" if election.kind == "initial" else "Смена старосты"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Одобрить", callback_data=f"election:approve:{election.id}"),
                    InlineKeyboardButton("❌ Отклонить", callback_data=f"election:reject:{election.id}"),
                ]
            ]
        )
        text = f"{kind_label}: {elections.format_candidate_label(candidate)} ({count}/2)"
        await reply(update, text, reply_markup=keyboard)

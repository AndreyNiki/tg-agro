import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.starosta_commands import (
    election_approve_callback,
    election_reject_callback,
    nominate_self_callback,
    starosta_command,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_group(conn, name="Б-Мв-101"):
    return storage.create_group(
        conn, name=name, source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )


def _add_members(conn, group_id, chat_ids):
    for chat_id in chat_ids:
        storage.upsert_member(conn, chat_id, group_id, joined_at="t")


def make_message_update(chat_id):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


def make_callback_update(chat_id, data):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    return update


def make_context(conn):
    context = MagicMock()
    context.bot_data = {"conn": conn}
    context.bot.send_message = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_starosta_command_offers_nomination_button_when_none_and_enough_members(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    update = make_message_update(111)
    context = make_context(conn)

    await starosta_command(update, context)

    _, kwargs = update.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_starosta_command_refuses_nomination_with_too_few_members(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222])
    update = make_message_update(111)
    context = make_context(conn)

    await starosta_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "недостаточно участников" in text


@pytest.mark.asyncio
async def test_nominate_self_callback_creates_election_and_broadcasts(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    update = make_callback_update(111, "starosta:nominate_self")
    context = make_context(conn)

    await nominate_self_callback(update, context)

    election = storage.get_open_election(conn, group_id)
    assert election is not None
    assert election.kind == "initial"
    assert election.candidate_chat_id == 111
    assert context.bot.send_message.await_count == 3  # broadcast to all 3 members


@pytest.mark.asyncio
async def test_nominate_self_callback_refuses_when_already_open(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    storage.create_election(conn, group_id, "initial", 222, 222, "t")
    update = make_callback_update(111, "starosta:nominate_self")
    context = make_context(conn)

    await nominate_self_callback(update, context)

    assert context.bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_election_approve_second_vote_applies_result(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")
    storage.add_election_approval(conn, election_id, 222, "t")

    update = make_callback_update(333, f"election:approve:{election_id}")
    context = make_context(conn)

    await election_approve_callback(update, context)

    group = storage.get_group(conn, group_id)
    assert group.starosta_chat_id == 111
    assert storage.get_election(conn, election_id).status == "approved"
    context.bot.send_message.assert_awaited_once()  # welcome message to the new starosta
    assert context.bot.send_message.await_args.kwargs["chat_id"] == 111


@pytest.mark.asyncio
async def test_election_approve_first_vote_does_not_apply_yet(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")

    update = make_callback_update(222, f"election:approve:{election_id}")
    context = make_context(conn)

    await election_approve_callback(update, context)

    assert storage.get_group(conn, group_id).starosta_chat_id is None
    assert storage.get_election(conn, election_id).status == "open"
    assert "1/2" in update.callback_query.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_election_approve_same_chat_id_twice_does_not_double_count(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")

    update = make_callback_update(222, f"election:approve:{election_id}")
    context = make_context(conn)

    await election_approve_callback(update, context)
    await election_approve_callback(update, context)

    assert storage.count_election_approvals(conn, election_id) == 1
    assert storage.get_group(conn, group_id).starosta_chat_id is None


@pytest.mark.asyncio
async def test_election_reject_does_not_write_anything(conn):
    group_id = _make_group(conn)
    _add_members(conn, group_id, [111, 222, 333])
    election_id = storage.create_election(conn, group_id, "initial", 111, 111, "t")

    update = make_callback_update(222, f"election:reject:{election_id}")
    update.callback_query.answer = AsyncMock()
    context = make_context(conn)

    await election_reject_callback(update, context)

    assert storage.count_election_approvals(conn, election_id) == 0
    assert storage.get_election(conn, election_id).status == "open"
    update.callback_query.answer.assert_awaited_once()

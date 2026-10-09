import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.change_group_commands import (
    change_group_command,
    change_group_confirm_callback,
    change_group_select_callback,
    change_request_approve_callback,
    change_request_reject_callback,
)

ADMIN_CHAT_ID = 999


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_group(conn, name):
    return storage.create_group(
        conn, name=name, source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )


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
    context.bot_data = {"conn": conn, "admin_chat_id": ADMIN_CHAT_ID}
    context.bot.send_message = AsyncMock()
    return context


@pytest.mark.asyncio
async def test_change_group_command_shows_picker_excluding_current_group(conn):
    group_a = _make_group(conn, "A")
    _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    update = make_message_update(111)
    context = make_context(conn)

    await change_group_command(update, context)

    _, kwargs = update.message.reply_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert "A" not in texts
    assert "B" in texts


@pytest.mark.asyncio
async def test_change_group_command_refuses_second_pending_request(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_message_update(111)
    context = make_context(conn)

    await change_group_command(update, context)

    assert "уже есть заявка" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_select_callback_shows_confirmation(conn):
    group_b = _make_group(conn, "B")
    update = make_callback_update(111, f"chggrp:select:{group_b}")
    context = make_context(conn)

    await change_group_select_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    _, kwargs = update.callback_query.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_confirm_callback_creates_request_and_notifies_admin_only_without_starosta(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    update = make_callback_update(111, f"chgconfirm:{group_b}")
    context = make_context(conn)

    await change_group_confirm_callback(update, context)

    request = storage.get_pending_request_for_chat(conn, 111)
    assert request is not None
    assert request.to_group_id == group_b
    context.bot.send_message.assert_awaited_once()
    assert context.bot.send_message.await_args.kwargs["chat_id"] == ADMIN_CHAT_ID


@pytest.mark.asyncio
async def test_confirm_callback_notifies_target_starosta_too(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    update = make_callback_update(111, f"chgconfirm:{group_b}")
    context = make_context(conn)

    await change_group_confirm_callback(update, context)

    notified_chat_ids = {call.kwargs["chat_id"] for call in context.bot.send_message.await_args_list}
    assert notified_chat_ids == {ADMIN_CHAT_ID, 555}


@pytest.mark.asyncio
async def test_confirm_callback_refuses_second_pending_request(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(111, f"chgconfirm:{group_b}")
    context = make_context(conn)

    await change_group_confirm_callback(update, context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_change_request_approve_by_admin_moves_member(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(ADMIN_CHAT_ID, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_b
    assert storage.get_group_change_request(conn, request_id).status == "approved"
    assert context.bot.send_message.await_args.kwargs["chat_id"] == 111


@pytest.mark.asyncio
async def test_change_request_approve_by_target_starosta_moves_member(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(555, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_b


@pytest.mark.asyncio
async def test_change_request_approve_rejects_unauthorized_caller(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(777, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_a
    assert storage.get_group_change_request(conn, request_id).status == "pending"


@pytest.mark.asyncio
async def test_change_request_approve_clears_starosta_of_origin_group(conn):
    # Finding 2 regression: if the requester was the starosta of their OLD
    # group, that group must not keep pointing at a departed starosta.
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.update_group_starosta(conn, group_a, 111)
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(ADMIN_CHAT_ID, f"chgreq:approve:{request_id}")
    context = make_context(conn)

    await change_request_approve_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_b
    assert storage.get_group(conn, group_a).starosta_chat_id is None


@pytest.mark.asyncio
async def test_change_request_reject_by_admin_keeps_member_in_place(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    request_id = storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(ADMIN_CHAT_ID, f"chgreq:reject:{request_id}")
    context = make_context(conn)

    await change_request_reject_callback(update, context)

    assert storage.get_member(conn, 111).group_id == group_a
    assert storage.get_group_change_request(conn, request_id).status == "rejected"

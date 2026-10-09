import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.join_requests import create_join_request_and_notify, notify_approvers, requires_approval

ADMIN_CHAT_ID = 999


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


def test_requires_approval_false_for_open_group_without_exclusion(conn):
    group_id = _make_group(conn)
    group = storage.get_group(conn, group_id)
    assert requires_approval(conn, group, 111) is False


def test_requires_approval_true_for_closed_group(conn):
    group_id = _make_group(conn)
    storage.update_group_closed(conn, group_id, True)
    group = storage.get_group(conn, group_id)
    assert requires_approval(conn, group, 111) is True


def test_requires_approval_true_for_excluded_chat_even_if_group_open(conn):
    group_id = _make_group(conn)
    storage.create_group_exclusion(conn, group_id, 111, "t", ADMIN_CHAT_ID)
    group = storage.get_group(conn, group_id)
    assert requires_approval(conn, group, 111) is True


@pytest.mark.asyncio
async def test_notify_approvers_reaches_admin_and_starosta(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 555, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 555)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    group = storage.get_group(conn, group_id)
    request_id = storage.create_group_change_request(conn, 111, None, group_id, "t")
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await notify_approvers(bot, conn, group, request_id, 111, ADMIN_CHAT_ID)

    notified = {call.kwargs["chat_id"] for call in bot.send_message.await_args_list}
    assert notified == {ADMIN_CHAT_ID, 555}


@pytest.mark.asyncio
async def test_notify_approvers_admin_only_when_no_starosta(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    group = storage.get_group(conn, group_id)
    request_id = storage.create_group_change_request(conn, 111, None, group_id, "t")
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await notify_approvers(bot, conn, group, request_id, 111, ADMIN_CHAT_ID)

    notified = {call.kwargs["chat_id"] for call in bot.send_message.await_args_list}
    assert notified == {ADMIN_CHAT_ID}


@pytest.mark.asyncio
async def test_notify_approvers_uses_explicit_requester_label_without_db_lookup(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    group = storage.get_group(conn, group_id)
    # No members row exists for the requester chat_id (222) — a get_member
    # lookup would render "Неизвестный". An explicit label must be used
    # instead and must not be overridden by a lookup.
    request_id = storage.create_group_change_request(conn, 222, None, group_id, "t")
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await notify_approvers(bot, conn, group, request_id, 222, ADMIN_CHAT_ID, requester_label="Ivan Ivanov (@ivan)")

    text = bot.send_message.await_args.kwargs["text"]
    assert "Ivan Ivanov (@ivan)" in text
    assert "Неизвестный" not in text


@pytest.mark.asyncio
async def test_notify_approvers_wording_join_vs_transfer(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    group = storage.get_group(conn, group_id)
    request_id = storage.create_group_change_request(conn, 111, None, group_id, "t")
    bot = MagicMock()
    bot.send_message = AsyncMock()

    await notify_approvers(bot, conn, group, request_id, 111, ADMIN_CHAT_ID, from_group_id=None)
    text_join = bot.send_message.await_args.kwargs["text"]
    assert "вступление" in text_join
    assert "переход" not in text_join

    bot.send_message.reset_mock()
    await notify_approvers(bot, conn, group, request_id, 111, ADMIN_CHAT_ID, from_group_id=777)
    text_transfer = bot.send_message.await_args.kwargs["text"]
    assert "переход" in text_transfer


@pytest.mark.asyncio
async def test_create_join_request_and_notify_creates_pending_row(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    group = storage.get_group(conn, group_id)
    bot = MagicMock()
    bot.send_message = AsyncMock()

    request_id = await create_join_request_and_notify(bot, conn, 222, None, group, ADMIN_CHAT_ID)

    request = storage.get_group_change_request(conn, request_id)
    assert request.status == "pending"
    assert request.from_group_id is None
    assert request.to_group_id == group_id

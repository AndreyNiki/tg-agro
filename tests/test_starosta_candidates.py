import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.starosta_candidates import (
    CANDIDATES_PER_PAGE,
    abort_callback,
    build_candidate_keyboard,
    confirm_callback,
    page_callback,
    select_callback,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_group_with_members(conn, count):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    for i in range(count):
        storage.upsert_member(conn, 100 + i, group_id, joined_at="t", subscribed=True)
        storage.update_member_profile(conn, 100 + i, telegram_username=f"user{i}", display_name=None)
    return group_id


def make_callback_update(chat_id, data):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.edit_reply_markup = AsyncMock()
    return update


def make_context(conn):
    context = MagicMock()
    context.bot_data = {"conn": conn}
    context.bot.send_message = AsyncMock()
    return context


def test_build_candidate_keyboard_paginates(conn):
    group_id = _make_group_with_members(conn, CANDIDATES_PER_PAGE + 2)
    first_page = build_candidate_keyboard(conn, group_id, page=0)
    buttons = [b for row in first_page.inline_keyboard for b in row if b.callback_data.startswith("starcand:select:")]
    assert len(buttons) == CANDIDATES_PER_PAGE


@pytest.mark.asyncio
async def test_page_callback_updates_keyboard(conn):
    group_id = _make_group_with_members(conn, CANDIDATES_PER_PAGE + 2)
    storage.upsert_member(conn, 999, group_id, joined_at="t")
    update = make_callback_update(999, "starcand:page:1")
    context = make_context(conn)

    await page_callback(update, context)

    update.callback_query.message.edit_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_select_callback_shows_confirmation(conn):
    group_id = _make_group_with_members(conn, 3)
    update = make_callback_update(100, "starcand:select:101")
    context = make_context(conn)

    await select_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    _, kwargs = update.callback_query.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_abort_callback_replies_cancelled(conn):
    update = make_callback_update(100, "starcand:abort")
    context = make_context(conn)

    await abort_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_confirm_callback_creates_election_and_broadcasts(conn):
    group_id = _make_group_with_members(conn, 3)
    update = make_callback_update(100, "starcand:confirm:101")
    context = make_context(conn)

    await confirm_callback(update, context)

    election = storage.get_open_election(conn, group_id)
    assert election is not None
    assert election.kind == "change"
    assert election.candidate_chat_id == 101
    assert context.bot.send_message.await_count == 3


@pytest.mark.asyncio
async def test_confirm_callback_refuses_when_vote_already_started_meanwhile(conn):
    group_id = _make_group_with_members(conn, 3)
    storage.create_election(conn, group_id, "change", 102, 102, "t")
    update = make_callback_update(100, "starcand:confirm:101")
    context = make_context(conn)

    await confirm_callback(update, context)

    assert context.bot.send_message.await_count == 0


@pytest.mark.asyncio
async def test_confirm_callback_refuses_when_membership_shrinks_below_minimum(conn):
    group_id = _make_group_with_members(conn, 3)
    other_group_id = storage.create_group(
        conn, name="Б-Мв-102", source_page_url="u2", pdf_source_url="p2",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    # Move members away so the original group drops below the minimum size
    # required for a vote, simulating a shrink that happens between showing
    # the candidate keyboard and the user clicking confirm.
    storage.upsert_member(conn, 101, other_group_id, joined_at="t", subscribed=True)
    storage.upsert_member(conn, 102, other_group_id, joined_at="t", subscribed=True)

    update = make_callback_update(100, "starcand:confirm:101")
    context = make_context(conn)

    await confirm_callback(update, context)

    assert storage.get_open_election(conn, group_id) is None
    assert context.bot.send_message.await_count == 0

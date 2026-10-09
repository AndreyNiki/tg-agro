from schedule_bot.classmates import format_member_line
from schedule_bot.storage import MemberRow


def _member(**overrides):
    defaults = dict(
        chat_id=111, group_id=1, telegram_username=None, display_name=None, subscribed=True, joined_at="t"
    )
    defaults.update(overrides)
    return MemberRow(**defaults)


def test_format_member_line_with_both_fields():
    member = _member(display_name="Ivan Ivanov", telegram_username="ivan")
    assert format_member_line(member, is_starosta=False) == "Ivan Ivanov (@ivan)"


def test_format_member_line_with_only_display_name():
    member = _member(display_name="Ivan Ivanov")
    assert format_member_line(member, is_starosta=False) == "Ivan Ivanov"


def test_format_member_line_with_only_username():
    member = _member(telegram_username="ivan")
    assert format_member_line(member, is_starosta=False) == "@ivan"


def test_format_member_line_with_neither():
    member = _member()
    assert format_member_line(member, is_starosta=False) == "Неизвестный"


def test_format_member_line_marks_starosta():
    member = _member(display_name="Ivan Ivanov")
    assert format_member_line(member, is_starosta=True) == "👑 Ivan Ivanov"


def test_format_member_line_no_mark_when_not_starosta():
    member = _member(display_name="Ivan Ivanov")
    line = format_member_line(member, is_starosta=False)
    assert "👑" not in line


import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.classmates import classmates_command


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def make_update(chat_id):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    return update


def make_context(conn):
    context = MagicMock()
    context.bot_data = {"conn": conn}
    return context


@pytest.mark.asyncio
async def test_classmates_command_without_group_prompts_start(conn):
    update = make_update(111)
    context = make_context(conn)

    await classmates_command(update, context)

    assert "/start" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_classmates_command_lists_sorted_members_with_starosta_mark_and_legend(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_member_profile(conn, 111, telegram_username="boris", display_name=None)
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    storage.update_member_profile(conn, 222, telegram_username="anna", display_name=None)
    storage.update_group_starosta(conn, group_id, 111)

    update = make_update(222)
    context = make_context(conn)

    await classmates_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    lines = text.split("\n")
    assert lines[0] == "@anna"
    assert lines[1] == "👑 @boris"
    assert "👑 — староста группы" in text


@pytest.mark.asyncio
async def test_classmates_command_no_legend_when_no_starosta(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_member_profile(conn, 111, telegram_username="boris", display_name=None)

    update = make_update(111)
    context = make_context(conn)

    await classmates_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "👑" not in text


@pytest.mark.asyncio
async def test_classmates_command_includes_unsubscribed_members(conn):
    group_id = storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    storage.upsert_member(conn, 111, group_id, joined_at="t", subscribed=True)
    storage.update_member_profile(conn, 111, telegram_username="boris", display_name=None)
    storage.upsert_member(conn, 222, group_id, joined_at="t", subscribed=False)
    storage.update_member_profile(conn, 222, telegram_username="anna", display_name=None)

    update = make_update(111)
    context = make_context(conn)

    await classmates_command(update, context)

    text = update.message.reply_text.call_args[0][0]
    assert "@anna" in text
    assert "@boris" in text

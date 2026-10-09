import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.report import _apply_report, format_report_message, report_command

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


def test_format_report_message_with_known_reporter_and_group(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_member_profile(conn, 111, telegram_username="ivan", display_name=None)
    reporter = storage.get_member(conn, 111)
    group = storage.get_group(conn, group_id)

    text = format_report_message(reporter, group, 111, "Бот сломался")

    assert "@ivan" in text
    assert "Б-Мв-101" in text
    assert "Бот сломался" in text
    assert "111" in text


def test_format_report_message_with_unknown_reporter_and_no_group():
    text = format_report_message(None, None, 222, "Помогите")
    assert "Неизвестный" in text
    assert "Помогите" in text


@pytest.mark.asyncio
async def test_apply_report_forwards_to_admin(conn):
    bot_client = MagicMock()
    bot_client.send_message = AsyncMock()

    result = await _apply_report(conn, bot_client, ADMIN_CHAT_ID, 111, "Проблема с расписанием")

    assert "отправлено" in result
    bot_client.send_message.assert_awaited_once()
    assert bot_client.send_message.await_args.kwargs["chat_id"] == ADMIN_CHAT_ID
    assert "Проблема с расписанием" in bot_client.send_message.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_report_command_without_args_asks_for_usage(conn):
    update = MagicMock()
    update.effective_chat.id = 111
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot_data = {"conn": conn, "admin_chat_id": ADMIN_CHAT_ID}
    context.args = []

    await report_command(update, context)

    assert "Использование" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_report_command_forwards_joined_text(conn):
    update = MagicMock()
    update.effective_chat.id = 111
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot_data = {"conn": conn, "admin_chat_id": ADMIN_CHAT_ID}
    context.args = ["Бот", "не", "отвечает"]
    context.bot.send_message = AsyncMock()

    await report_command(update, context)

    assert "Бот не отвечает" in context.bot.send_message.await_args.kwargs["text"]
    update.message.reply_text.assert_awaited_once()

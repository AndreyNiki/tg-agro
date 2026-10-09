import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.menu import build_root_keyboard, menu_command

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


def _button_texts(keyboard):
    return [b.text for row in keyboard.inline_keyboard for b in row]


def test_root_keyboard_plain_member_sees_schedule_group_starosta_not_admin(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")

    keyboard = build_root_keyboard(conn, 111, ADMIN_CHAT_ID)
    texts = _button_texts(keyboard)

    assert any("Расписание" in t for t in texts)
    assert any("Группа" in t for t in texts)
    assert any("Староста" in t for t in texts)
    assert not any("Админ" in t for t in texts)


def test_root_keyboard_no_group_hides_schedule_group_and_starosta(conn):
    keyboard = build_root_keyboard(conn, 111, ADMIN_CHAT_ID)
    texts = _button_texts(keyboard)
    assert not any(t in ("📅 Расписание", "👥 Группа", "👑 Староста") for t in texts)
    # Task 10 adds FAQ/Report unconditionally, so the keyboard isn't empty anymore —
    # this test only asserts the group-gated sections are absent.


def test_root_keyboard_admin_sees_admin_section(conn):
    keyboard = build_root_keyboard(conn, ADMIN_CHAT_ID, ADMIN_CHAT_ID)
    texts = _button_texts(keyboard)
    assert any("Админ" in t for t in texts)


@pytest.mark.asyncio
async def test_menu_command_replies_with_root_keyboard(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = MagicMock()
    update.effective_chat.id = 111
    update.message.reply_text = AsyncMock()
    context = MagicMock()
    context.bot_data = {"conn": conn, "admin_chat_id": ADMIN_CHAT_ID}

    await menu_command(update, context)

    update.message.reply_text.assert_awaited_once()
    _, kwargs = update.message.reply_text.call_args
    assert "reply_markup" in kwargs

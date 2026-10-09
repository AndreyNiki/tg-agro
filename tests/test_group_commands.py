import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.group_commands import (
    GROUPS_PER_PAGE,
    build_group_picker_keyboard,
    group_page_callback,
    group_select_callback,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_groups(conn, count):
    for i in range(count):
        storage.create_group(
            conn, name=f"Группа-{i:02d}", source_page_url="u", pdf_source_url="p",
            send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
        )


def test_build_group_picker_keyboard_shows_one_button_per_group_plus_create(conn):
    _make_groups(conn, 3)
    keyboard = build_group_picker_keyboard(conn)
    texts = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "Группа-00" in texts
    assert "Группа-01" in texts
    assert "Группа-02" in texts
    assert any("Создать" in text for text in texts)


def test_build_group_picker_keyboard_paginates(conn):
    _make_groups(conn, GROUPS_PER_PAGE + 2)
    first_page = build_group_picker_keyboard(conn, page=0)
    first_page_names = [
        button.text for row in first_page.inline_keyboard for button in row
        if button.callback_data.startswith("grp:select:")
    ]
    assert len(first_page_names) == GROUPS_PER_PAGE
    nav_data = [
        button.callback_data for row in first_page.inline_keyboard for button in row
        if button.callback_data.startswith("grp:page:")
    ]
    assert nav_data == ["grp:page:1"]  # no "previous" on page 0, "next" present

    second_page = build_group_picker_keyboard(conn, page=1)
    second_page_names = [
        button.text for row in second_page.inline_keyboard for button in row
        if button.callback_data.startswith("grp:select:")
    ]
    assert len(second_page_names) == 2
    nav_data_2 = [
        button.callback_data for row in second_page.inline_keyboard for button in row
        if button.callback_data.startswith("grp:page:")
    ]
    assert nav_data_2 == ["grp:page:0"]  # only "previous" on the last page


def make_callback_update(chat_id, data, username="ivan", full_name="Ivan Ivanov"):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.username = username
    update.effective_user.full_name = full_name
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.edit_reply_markup = AsyncMock()
    return update


def make_context(conn):
    context = MagicMock()
    context.bot_data = {"conn": conn}
    return context


@pytest.mark.asyncio
async def test_group_page_callback_updates_keyboard(conn):
    _make_groups(conn, GROUPS_PER_PAGE + 2)
    update = make_callback_update(111, "grp:page:1")
    context = make_context(conn)

    await group_page_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.edit_reply_markup.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_select_callback_attaches_member_and_replies(conn):
    _make_groups(conn, 1)
    group_id = storage.list_all_groups(conn)[0].id
    update = make_callback_update(111, f"grp:select:{group_id}")
    context = make_context(conn)

    await group_select_callback(update, context)

    member = storage.get_member(conn, 111)
    assert member.group_id == group_id
    assert member.subscribed is True
    assert member.telegram_username == "ivan"
    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_select_callback_rejects_deleted_group(conn):
    update = make_callback_update(111, "grp:select:9999")
    context = make_context(conn)

    await group_select_callback(update, context)

    assert storage.get_member(conn, 111) is None
    update.callback_query.message.reply_text.assert_awaited_once()

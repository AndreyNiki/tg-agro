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


from schedule_bot.menu import menu_callback


def make_callback_update(chat_id, data):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    update.callback_query.message = None
    update.message = None
    update.callback_query.edit_message_text = AsyncMock()
    return update


def make_context(conn):
    context = MagicMock()
    context.bot_data = {"conn": conn, "admin_chat_id": ADMIN_CHAT_ID}
    return context


@pytest.mark.asyncio
async def test_menu_callback_root_edits_to_root_keyboard(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_callback_update(111, "menu:root:_")
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.edit_message_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_callback_schedule_root_shows_section_keyboard(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_callback_update(111, "menu:schedule:root")
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.edit_message_text.assert_awaited_once()
    _, kwargs = update.callback_query.edit_message_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any("Выбрать день" in t for t in texts)
    assert any("Подпис" in t or "Отпис" in t for t in texts)


@pytest.mark.asyncio
async def test_menu_callback_schedule_day_picker_calls_schedule_command(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_callback_update(111, "menu:schedule:daypicker")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_callback_schedule_toggle_subscribe_calls_subscribe_when_unsubscribed(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t", subscribed=False)
    update = make_callback_update(111, "menu:schedule:togglesub")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    assert storage.get_member(conn, 111).subscribed is True


@pytest.mark.asyncio
async def test_menu_callback_group_root_shows_section_keyboard(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_callback_update(111, "menu:group:root")
    context = make_context(conn)

    await menu_callback(update, context)

    _, kwargs = update.callback_query.edit_message_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any("Одногруппники" in t for t in texts)
    assert any("Сменить" in t for t in texts)
    assert any("голосования" in t for t in texts)


@pytest.mark.asyncio
async def test_menu_callback_group_classmates_calls_classmates_command(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_callback_update(111, "menu:group:classmates")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_callback_group_votes_calls_votes_command(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_callback_update(111, "menu:group:votes")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_callback_group_changegroup_calls_change_group_command(conn):
    group_a = _make_group(conn, "A")
    _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    update = make_callback_update(111, "menu:group:changegroup")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()

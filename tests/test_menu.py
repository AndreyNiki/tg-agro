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


@pytest.mark.asyncio
async def test_menu_callback_starosta_root_delegates_to_starosta_command_when_not_starosta(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    storage.upsert_member(conn, 333, group_id, joined_at="t")
    update = make_callback_update(111, "menu:starosta:root")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_callback_starosta_root_shows_privileges_when_caller_is_starosta(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    update = make_callback_update(111, "menu:starosta:root")
    context = make_context(conn)

    await menu_callback(update, context)

    _, kwargs = update.callback_query.edit_message_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any("Исключить" in t for t in texts)
    assert any("Закрыть" in t or "Открыть" in t for t in texts)
    assert any("Оповестить" in t for t in texts)
    assert any("Заявки" in t for t in texts)


@pytest.mark.asyncio
async def test_menu_callback_starosta_toggleclosed_flips_state(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    update = make_callback_update(111, "menu:starosta:toggleclosed")
    context = make_context(conn)

    await menu_callback(update, context)

    assert storage.get_group(conn, group_id).is_closed is True


@pytest.mark.asyncio
async def test_menu_callback_starosta_exclude_shows_classmates_with_buttons(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    update = make_callback_update(111, "menu:starosta:exclude")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    _, kwargs = update.callback_query.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_menu_callback_starosta_requests_lists_pending(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 555, group_b, joined_at="t")
    storage.update_group_starosta(conn, group_b, 555)
    storage.create_group_change_request(conn, 111, group_a, group_b, "t")
    update = make_callback_update(555, "menu:starosta:requests")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_callback_starosta_notify_prompts_for_text(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 111)
    update = make_callback_update(111, "menu:starosta:notify")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}

    await menu_callback(update, context)

    assert context.user_data["pending_menu_action"] == {"action": "notify", "group_id": group_id}
    update.callback_query.message.reply_text.assert_awaited_once()


from schedule_bot.menu import pending_action_followup


@pytest.mark.asyncio
async def test_menu_callback_admin_root_shows_admin_keyboard(conn):
    update = make_callback_update(ADMIN_CHAT_ID, "menu:admin:root")
    context = make_context(conn)

    await menu_callback(update, context)

    _, kwargs = update.callback_query.edit_message_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any("Объявление" in t for t in texts)
    assert any("Настройки группы" in t for t in texts)
    assert any("Старосты" in t for t in texts)
    assert any("Открытость" in t for t in texts)
    assert any("Заявки" in t for t in texts)


@pytest.mark.asyncio
async def test_menu_callback_admin_alert_prompts_for_text(conn):
    update = make_callback_update(ADMIN_CHAT_ID, "menu:admin:alert")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}

    await menu_callback(update, context)

    assert context.user_data["pending_menu_action"] == {"action": "alert"}


@pytest.mark.asyncio
async def test_menu_callback_admin_groupsettings_shows_group_picker(conn):
    _make_group(conn, "A")
    update = make_callback_update(ADMIN_CHAT_ID, "menu:admin:groupsettings")
    context = make_context(conn)

    await menu_callback(update, context)

    _, kwargs = update.callback_query.edit_message_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert "A" in texts


@pytest.mark.asyncio
async def test_menu_callback_admin_pickgroup_shows_group_settings_submenu(conn):
    group_id = _make_group(conn, "A")
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:pickgroup:{group_id}")
    context = make_context(conn)

    await menu_callback(update, context)

    _, kwargs = update.callback_query.edit_message_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any("PDF" in t for t in texts)
    assert any("рассылки" in t for t in texts)
    assert any("Интервал" in t for t in texts)


@pytest.mark.asyncio
async def test_menu_callback_admin_setpdf_prompts_with_group_id(conn):
    group_id = _make_group(conn, "A")
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:setpdf:{group_id}")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}

    await menu_callback(update, context)

    assert context.user_data["pending_menu_action"] == {"action": "set_pdf_source", "group_id": group_id}


@pytest.mark.asyncio
async def test_menu_callback_admin_toggleclosed_flips_picked_group(conn):
    group_id = _make_group(conn, "A")
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:toggleclosed:{group_id}")
    context = make_context(conn)

    await menu_callback(update, context)

    assert storage.get_group(conn, group_id).is_closed is True


@pytest.mark.asyncio
async def test_menu_callback_admin_removestarosta_clears_picked_group(conn):
    group_id = _make_group(conn, "A")
    storage.update_group_starosta(conn, group_id, 111)
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:removestarosta:{group_id}")
    context = make_context(conn)

    await menu_callback(update, context)

    assert storage.get_group(conn, group_id).starosta_chat_id is None


@pytest.mark.asyncio
async def test_pending_action_followup_applies_set_send_time(conn):
    group_id = _make_group(conn, "A")
    update = MagicMock()
    update.effective_chat.id = ADMIN_CHAT_ID
    update.message.text = "07:45"
    update.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {"pending_menu_action": {"action": "set_send_time", "group_id": group_id}}
    context.bot_data["reschedule_group_jobs"] = MagicMock()

    await pending_action_followup(update, context)

    assert storage.get_group(conn, group_id).send_time == "07:45"
    assert "pending_menu_action" not in context.user_data
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_action_followup_applies_alert_globally(conn):
    group_id = _make_group(conn, "A")
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = MagicMock()
    update.effective_chat.id = ADMIN_CHAT_ID
    update.message.text = "Обновление"
    update.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {"pending_menu_action": {"action": "alert"}}
    context.bot.send_message = AsyncMock()

    await pending_action_followup(update, context)

    context.bot.send_message.assert_awaited_once()
    assert "pending_menu_action" not in context.user_data

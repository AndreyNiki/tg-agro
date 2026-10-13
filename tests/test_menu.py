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


def _make_institute(conn, name="ИТ"):
    return storage.create_institute(
        conn, name=name, catalog_url="old", check_interval_minutes=60, send_time="09:00",
        tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )


_group_counter = [0]


def _make_group(conn, name="Б-Мв-101"):
    _group_counter[0] += 1
    institute_id = _make_institute(conn, name=f"ИТ-{_group_counter[0]}")
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url=f"https://example.com/{_group_counter[0]}.pdf",
        group_names=[name], raw_label=name, last_seen_at="t",
    )
    return storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name=name)


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
async def test_menu_callback_admin_requests_shows_submenu(conn):
    update = make_callback_update(ADMIN_CHAT_ID, "menu:admin:requests")
    context = make_context(conn)

    await menu_callback(update, context)

    _, kwargs = update.callback_query.edit_message_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert "📋 Все заявки" in texts
    assert "🔎 По группе" in texts


@pytest.mark.asyncio
async def test_menu_callback_admin_requestsbygroup_shows_group_picker(conn):
    _make_group(conn, "A")
    update = make_callback_update(ADMIN_CHAT_ID, "menu:admin:requestsbygroup")
    context = make_context(conn)

    await menu_callback(update, context)

    _, kwargs = update.callback_query.edit_message_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert "A" in texts


@pytest.mark.asyncio
async def test_menu_callback_admin_allrequests_lists_pending_across_groups(conn):
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.create_group_change_request(conn, 111, None, group_a, "t1")
    storage.create_group_change_request(conn, 222, None, group_b, "t2")
    update = make_callback_update(ADMIN_CHAT_ID, "menu:admin:allrequests")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    assert update.callback_query.message.reply_text.await_count == 2
    texts = [call.args[0] for call in update.callback_query.message.reply_text.await_args_list]
    assert any("«A»" in t for t in texts)
    assert any("«B»" in t for t in texts)


@pytest.mark.asyncio
async def test_menu_callback_admin_pickgroup_shows_group_settings_submenu(conn):
    group_id = _make_group(conn, "A")
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:pickgroup:{group_id}")
    context = make_context(conn)

    await menu_callback(update, context)

    _, kwargs = update.callback_query.edit_message_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any("Назад" in t for t in texts)
    assert "Источник PDF" not in texts
    assert "Время рассылки" not in texts
    assert "Интервал проверки" not in texts


@pytest.mark.asyncio
async def test_admin_root_keyboard_has_institutes_entry():
    from schedule_bot.menu import _admin_root_keyboard
    keyboard = _admin_root_keyboard()
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert any("Институт" in t for t in texts)


@pytest.mark.asyncio
async def test_group_settings_submenu_no_longer_offers_pdf_sendtime_interval():
    from schedule_bot.menu import _group_settings_submenu
    keyboard = _group_settings_submenu(1)
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "Источник PDF" not in texts
    assert "Время рассылки" not in texts
    assert "Интервал проверки" not in texts


@pytest.mark.asyncio
async def test_institute_scoped_pick_sets_pending_catalog_url_action(conn):
    institute_id = _make_institute(conn)
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:setinstitutecatalogurl:{institute_id}")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}

    await menu_callback(update, context)

    assert context.user_data["pending_menu_action"] == {
        "action": "set_institute_catalog_url", "institute_id": institute_id,
    }


@pytest.mark.asyncio
async def test_menu_rescan_button_reports_unreachable_catalog_instead_of_raising(conn, monkeypatch):
    """The rescan button had no try/except (unlike
    /refresh_institute_links), so an unreachable catalog page crashed the
    callback handler with an unhandled exception."""
    import schedule_bot.menu as menu_module

    institute_id = _make_institute(conn)
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:refreshinstitutelinks:{institute_id}")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}
    context.bot_data["reschedule_institute_jobs"] = MagicMock()

    def boom(*args, **kwargs):
        raise RuntimeError("catalog page unreachable")

    monkeypatch.setattr(menu_module, "refresh_institute_catalog", boom)

    await menu_callback(update, context)  # must not raise

    texts = [call.args[0] for call in update.callback_query.message.reply_text.await_args_list]
    assert any("не удалось" in text for text in texts)
    context.bot_data["reschedule_institute_jobs"].assert_not_called()


@pytest.mark.asyncio
async def test_menu_rescan_button_runs_blocking_fetch_off_the_event_loop(conn, monkeypatch):
    """Spec 3.2: with concurrent_updates(True), the ~15s blocking catalog
    fetch must not run on the event loop thread."""
    import threading

    import schedule_bot.menu as menu_module

    institute_id = _make_institute(conn)
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:refreshinstitutelinks:{institute_id}")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}
    context.bot_data["reschedule_institute_jobs"] = MagicMock()

    seen_threads = []
    monkeypatch.setattr(
        menu_module, "refresh_institute_catalog",
        lambda *a, **k: seen_threads.append(threading.current_thread()),
    )

    await menu_callback(update, context)

    assert seen_threads and seen_threads[0] is not threading.main_thread()


@pytest.mark.asyncio
async def test_pending_action_followup_applies_institute_catalog_url(conn):
    institute_id = _make_institute(conn)
    context = make_context(conn)
    context.user_data = {"pending_menu_action": {"action": "set_institute_catalog_url", "institute_id": institute_id}}
    update = MagicMock()
    update.message.text = "https://new.example.com"
    update.message.reply_text = AsyncMock()
    update.effective_chat.id = ADMIN_CHAT_ID

    await pending_action_followup(update, context)

    assert storage.get_institute(conn, institute_id).catalog_url == "https://new.example.com"


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
async def test_pending_action_followup_applies_institute_send_time(conn):
    institute_id = _make_institute(conn)
    update = MagicMock()
    update.effective_chat.id = ADMIN_CHAT_ID
    update.message.text = "07:45"
    update.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {"pending_menu_action": {"action": "set_institute_send_time", "institute_id": institute_id}}
    context.bot_data["reschedule_institute_link_jobs"] = MagicMock()

    await pending_action_followup(update, context)

    assert storage.get_institute(conn, institute_id).send_time == "07:45"
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


@pytest.mark.asyncio
async def test_menu_callback_toggleclosed_rejects_non_admin(conn):
    group_id = _make_group(conn, "A")
    update = make_callback_update(111, f"menu:toggleclosed:{group_id}")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    assert storage.get_group(conn, group_id).is_closed is False


@pytest.mark.asyncio
async def test_menu_callback_removestarosta_rejects_non_admin(conn):
    group_id = _make_group(conn, "A")
    storage.update_group_starosta(conn, group_id, 111)
    update = make_callback_update(222, f"menu:removestarosta:{group_id}")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    assert storage.get_group(conn, group_id).starosta_chat_id == 111


@pytest.mark.asyncio
async def test_menu_callback_setinstitutecatalogurl_rejects_non_admin_without_stashing_pending_action(conn):
    institute_id = _make_institute(conn)
    update = make_callback_update(111, f"menu:setinstitutecatalogurl:{institute_id}")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    assert "pending_menu_action" not in context.user_data


# --- Critical: stale pending_menu_action is cleared on navigation ---


@pytest.mark.asyncio
async def test_menu_callback_clears_stale_pending_action_on_unrelated_navigation(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_callback_update(111, "menu:root:_")
    context = make_context(conn)
    context.user_data = {"pending_menu_action": {"action": "alert"}}

    await menu_callback(update, context)

    assert "pending_menu_action" not in context.user_data


@pytest.mark.asyncio
async def test_menu_callback_clears_stale_pending_action_on_schedule_root(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_callback_update(111, "menu:schedule:root")
    context = make_context(conn)
    context.user_data = {"pending_menu_action": {"action": "notify", "group_id": group_id}}

    await menu_callback(update, context)

    assert "pending_menu_action" not in context.user_data


# --- Important: admin group-picker pagination ---


@pytest.mark.asyncio
async def test_menu_callback_pickgrouppage_rerenders_second_page(conn):
    for i in range(9):
        _make_group(conn, f"Group-{i}")
    update = make_callback_update(ADMIN_CHAT_ID, "menu:pickgrouppage:1")
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.edit_message_text.assert_awaited_once()
    _, kwargs = update.callback_query.edit_message_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert "Group-8" in texts


@pytest.mark.asyncio
async def test_menu_callback_toggleclosedpage_rejects_non_admin(conn):
    for i in range(9):
        _make_group(conn, f"Group-{i}")
    update = make_callback_update(111, "menu:toggleclosedpage:1")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.edit_message_text.assert_not_awaited()


# --- Important: admin dead-end fixes ---


@pytest.mark.asyncio
async def test_menu_callback_admin_toggleclosed_rerenders_picker_with_keyboard(conn):
    group_id = _make_group(conn, "A")
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:toggleclosed:{group_id}")
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.edit_message_text.assert_awaited_once()
    _, kwargs = update.callback_query.edit_message_text.call_args
    assert "reply_markup" in kwargs
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert "A" in texts


@pytest.mark.asyncio
async def test_menu_callback_admin_removestarosta_rerenders_starosta_submenu(conn):
    group_id = _make_group(conn, "A")
    storage.update_group_starosta(conn, group_id, 111)
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:removestarosta:{group_id}")
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.edit_message_text.assert_awaited_once()
    _, kwargs = update.callback_query.edit_message_text.call_args
    assert "reply_markup" in kwargs
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert any("Назначить" in t for t in texts)
    assert any("Снять" in t for t in texts)


# --- Important: notify follow-up re-verifies starosta privilege ---


@pytest.mark.asyncio
async def test_pending_action_followup_notify_rejects_if_no_longer_starosta(conn):
    group_id = _make_group(conn, "A")
    storage.upsert_member(conn, 777, group_id, joined_at="t")
    # 777 was starosta when the prompt was stashed, but has since been replaced.
    storage.update_group_starosta(conn, group_id, 999999)
    update = MagicMock()
    update.effective_chat.id = 777
    update.message.text = "Внимание всем"
    update.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {"pending_menu_action": {"action": "notify", "group_id": group_id}}
    context.bot.send_message = AsyncMock()

    await pending_action_followup(update, context)

    context.bot.send_message.assert_not_awaited()
    update.message.reply_text.assert_awaited_once()
    _, args, _kwargs = update.message.reply_text.mock_calls[0]
    assert "старост" in args[0].lower()


@pytest.mark.asyncio
async def test_pending_action_followup_notify_succeeds_if_still_starosta(conn):
    group_id = _make_group(conn, "A")
    storage.upsert_member(conn, 777, group_id, joined_at="t")
    storage.update_group_starosta(conn, group_id, 777)
    update = MagicMock()
    update.effective_chat.id = 777
    update.message.text = "Внимание всем"
    update.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {"pending_menu_action": {"action": "notify", "group_id": group_id}}
    context.bot.send_message = AsyncMock()

    await pending_action_followup(update, context)

    context.bot.send_message.assert_awaited()


# --- Test gaps: report / faq / no-pending-action ---


@pytest.mark.asyncio
async def test_menu_callback_report_start_stashes_pending_and_prompts(conn):
    update = make_callback_update(111, "menu:report:start")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}

    await menu_callback(update, context)

    assert context.user_data["pending_menu_action"] == {"action": "report"}
    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_callback_faq_show_replies_with_faq_text(conn):
    update = make_callback_update(111, "menu:faq:show")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.message = MagicMock()
    update.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    assert (
        update.callback_query.message.reply_text.await_count
        or update.message.reply_text.await_count
    )


@pytest.mark.asyncio
async def test_pending_action_followup_report_forwards_to_admin(conn):
    update = MagicMock()
    update.effective_chat.id = 111
    update.message.text = "У меня проблема"
    update.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {"pending_menu_action": {"action": "report"}}
    context.bot.send_message = AsyncMock()

    await pending_action_followup(update, context)

    context.bot.send_message.assert_awaited_once()
    call_kwargs = context.bot.send_message.call_args.kwargs
    assert call_kwargs.get("chat_id") == ADMIN_CHAT_ID
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_action_followup_does_nothing_without_pending_action(conn):
    update = MagicMock()
    update.effective_chat.id = 111
    update.message.text = "случайный текст"
    update.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}

    await pending_action_followup(update, context)

    update.message.reply_text.assert_not_awaited()


# --- Test gap: nested back button returns to immediate parent, not root ---


def test_group_settings_submenu_back_button_returns_to_groupsettings_not_root():
    from schedule_bot.menu import _group_settings_submenu

    keyboard = _group_settings_submenu(42)
    back_buttons = [b for row in keyboard.inline_keyboard for b in row if "Назад" in b.text]
    assert len(back_buttons) == 1
    assert back_buttons[0].callback_data == "menu:admin:groupsettings"


# --- New: every command reachable from the inline menu ---


def test_root_keyboard_non_member_shows_pick_institute_button(conn):
    keyboard = build_root_keyboard(conn, 111, ADMIN_CHAT_ID)
    texts = _button_texts(keyboard)
    assert any("Выбрать институт" in t for t in texts)


def test_root_keyboard_member_does_not_show_pick_institute_button(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    keyboard = build_root_keyboard(conn, 111, ADMIN_CHAT_ID)
    texts = _button_texts(keyboard)
    assert not any("Выбрать институт" in t for t in texts)


def test_root_keyboard_always_shows_help_button(conn):
    keyboard = build_root_keyboard(conn, 111, ADMIN_CHAT_ID)
    texts = _button_texts(keyboard)
    assert any("Список команд" in t for t in texts)


@pytest.mark.asyncio
async def test_menu_callback_start_pick_shows_institute_picker(conn):
    _make_institute(conn, "ИТ")
    update = make_callback_update(111, "menu:start:pick")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    _, kwargs = update.callback_query.message.reply_text.call_args
    texts = [b.text for row in kwargs["reply_markup"].inline_keyboard for b in row]
    assert "ИТ" in texts


@pytest.mark.asyncio
async def test_menu_callback_help_show_replies_with_help_text(conn):
    update = make_callback_update(111, "menu:help:show")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}

    await menu_callback(update, context)

    text = update.callback_query.message.reply_text.call_args.args[0]
    assert text.startswith("Доступные команды:")
    assert "/menu" in text
    assert "Команды администратора" not in text


@pytest.mark.asyncio
async def test_menu_callback_help_show_includes_admin_commands_for_admin(conn):
    update = make_callback_update(ADMIN_CHAT_ID, "menu:help:show")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}

    await menu_callback(update, context)

    text = update.callback_query.message.reply_text.call_args.args[0]
    assert "Команды администратора" in text


def test_institute_picker_keyboard_has_create_institute_button(conn):
    from schedule_bot.menu import _institute_picker_keyboard

    keyboard = _institute_picker_keyboard(conn)
    texts = _button_texts(keyboard)
    assert any("Создать институт" in t for t in texts)


@pytest.mark.asyncio
async def test_menu_callback_admin_createinstitute_stashes_pending_name(conn):
    update = make_callback_update(ADMIN_CHAT_ID, "menu:admin:createinstitute")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}

    await menu_callback(update, context)

    assert context.user_data["pending_menu_action"] == {"action": "create_institute_name"}
    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_menu_callback_admin_createinstitute_rejects_non_admin(conn):
    update = make_callback_update(111, "menu:admin:createinstitute")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}

    await menu_callback(update, context)

    assert "pending_menu_action" not in context.user_data


@pytest.mark.asyncio
async def test_pending_action_followup_create_institute_name_prompts_for_url(conn):
    update = MagicMock()
    update.effective_chat.id = ADMIN_CHAT_ID
    update.message.text = "Новый ИТ"
    update.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {"pending_menu_action": {"action": "create_institute_name"}}

    await pending_action_followup(update, context)

    assert context.user_data["pending_menu_action"] == {"action": "create_institute_url", "name": "Новый ИТ"}
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_pending_action_followup_create_institute_url_creates_and_reschedules(conn):
    update = MagicMock()
    update.effective_chat.id = ADMIN_CHAT_ID
    update.message.text = "https://example.com/catalog"
    update.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {"pending_menu_action": {"action": "create_institute_url", "name": "Новый ИТ"}}
    context.bot_data["config"] = MagicMock(
        default_daily_send_time="09:00", default_check_interval_minutes=60,
        default_timezone="Europe/Moscow", default_catalog_refresh_interval_minutes=10080,
    )
    context.bot_data["reschedule_institute_jobs"] = MagicMock()

    await pending_action_followup(update, context)

    institutes = storage.list_institutes(conn)
    assert any(i.name == "Новый ИТ" for i in institutes)
    context.bot_data["reschedule_institute_jobs"].assert_called_once()
    assert "pending_menu_action" not in context.user_data


def test_institute_settings_submenu_has_institute_links_button():
    from schedule_bot.menu import _institute_settings_submenu

    keyboard = _institute_settings_submenu(1)
    texts = _button_texts(keyboard)
    assert any("Ссылки каталога" in t for t in texts)


@pytest.mark.asyncio
async def test_menu_callback_institutelinks_lists_raw_labels_and_group_counts(conn):
    institute_id = _make_institute(conn)
    storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url="https://example.com/1.pdf",
        group_names=["A", "B"], raw_label="A,B", last_seen_at="t",
    )
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:institutelinks:{institute_id}")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once_with("'A,B' -> 2 групп")


def test_group_settings_submenu_has_refresh_and_notify_buttons():
    from schedule_bot.menu import _group_settings_submenu

    keyboard = _group_settings_submenu(42)
    texts = _button_texts(keyboard)
    assert any("Обновить расписание" in t for t in texts)
    assert any("Оповестить группу" in t for t in texts)


@pytest.mark.asyncio
async def test_menu_callback_refreshgroup_calls_sync_and_reports_result(conn):
    group_id = _make_group(conn, "A")
    group = storage.get_group(conn, group_id)
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:refreshgroup:{group_id}")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    class Result:
        def __init__(self, group_id, status):
            self.group_id = group_id
            self.status = status

    sync_fn = AsyncMock(return_value=[Result(group_id, "no_changes")])
    context.bot_data["sync_institute_link_fn"] = sync_fn

    await menu_callback(update, context)

    sync_fn.assert_awaited_once_with(group.institute_link_id)
    update.callback_query.message.reply_text.assert_awaited_once_with("Изменений нет.")


@pytest.mark.asyncio
async def test_menu_callback_refreshgroup_reports_sync_failure_without_raising(conn):
    group_id = _make_group(conn, "A")
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:refreshgroup:{group_id}")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    async def boom(_link_id):
        raise RuntimeError("fetch failed")

    context.bot_data["sync_institute_link_fn"] = boom

    await menu_callback(update, context)

    text = update.callback_query.message.reply_text.call_args.args[0]
    assert "не удалась" in text


@pytest.mark.asyncio
async def test_menu_callback_refreshgroup_rejects_non_admin(conn):
    group_id = _make_group(conn, "A")
    update = make_callback_update(111, f"menu:refreshgroup:{group_id}")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)

    await menu_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once_with("Недостаточно прав.")


@pytest.mark.asyncio
async def test_menu_callback_notifygroup_stashes_pending_notify_admin(conn):
    group_id = _make_group(conn, "A")
    update = make_callback_update(ADMIN_CHAT_ID, f"menu:notifygroup:{group_id}")
    update.callback_query.message = MagicMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {}

    await menu_callback(update, context)

    assert context.user_data["pending_menu_action"] == {"action": "notify_admin", "group_id": group_id}


@pytest.mark.asyncio
async def test_pending_action_followup_notify_admin_sends_to_group_members(conn):
    group_id = _make_group(conn, "A")
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = MagicMock()
    update.effective_chat.id = ADMIN_CHAT_ID
    update.message.text = "Внимание всем"
    update.message.reply_text = AsyncMock()
    context = make_context(conn)
    context.user_data = {"pending_menu_action": {"action": "notify_admin", "group_id": group_id}}
    context.bot.send_message = AsyncMock()

    await pending_action_followup(update, context)

    context.bot.send_message.assert_awaited()
    assert "pending_menu_action" not in context.user_data

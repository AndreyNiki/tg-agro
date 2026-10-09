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


from schedule_bot.group_commands import (
    ASK_GROUP_NAME,
    ASK_SOURCE_PAGE_URL,
    cancel_group_creation,
    receive_group_name,
    receive_source_page_url,
    start_group_creation,
)
from telegram.ext import ConversationHandler


def make_message_update(chat_id, text, username="ivan", full_name="Ivan Ivanov"):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.effective_user.username = username
    update.effective_user.full_name = full_name
    update.message.text = text
    update.message.reply_text = AsyncMock()
    return update


def make_context_with_config(conn, default_send_time="09:00", default_check_interval=60, default_timezone="Europe/Moscow"):
    context = MagicMock()
    context.user_data = {}
    context.bot_data = {
        "conn": conn,
        "config": MagicMock(
            default_daily_send_time=default_send_time,
            default_check_interval_minutes=default_check_interval,
            default_timezone=default_timezone,
        ),
        "reschedule_group_jobs": MagicMock(),
    }
    return context


@pytest.mark.asyncio
async def test_start_group_creation_asks_for_name(conn):
    update = make_callback_update(111, "grp:create")
    context = make_context_with_config(conn)

    state = await start_group_creation(update, context)

    assert state == ASK_GROUP_NAME
    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_receive_group_name_stores_name_and_asks_for_source_page(conn):
    update = make_message_update(111, "Б-Мв-999")
    context = make_context_with_config(conn)

    state = await receive_group_name(update, context)

    assert state == ASK_SOURCE_PAGE_URL
    assert context.user_data["new_group_name"] == "Б-Мв-999"


@pytest.mark.asyncio
async def test_receive_source_page_url_rejects_duplicate_group_name(conn):
    storage.create_group(
        conn, name="Б-Мв-101", source_page_url="u", pdf_source_url="p",
        send_time="09:00", check_interval_minutes=60, tz_name="Europe/Moscow",
    )
    update = make_message_update(111, "https://example.com/schedules")
    context = make_context_with_config(conn)
    context.user_data["new_group_name"] = "Б-Мв-101"

    state = await receive_source_page_url(update, context)

    assert state == ConversationHandler.END
    assert storage.count_groups(conn) == 1


@pytest.mark.asyncio
async def test_receive_source_page_url_reports_unreachable_source_page(conn, monkeypatch):
    import schedule_bot.group_commands as group_commands_module
    from schedule_bot.group_links import SourcePageUnreachableError

    monkeypatch.setattr(
        group_commands_module, "resolve_group_pdf_source",
        lambda *a, **k: (_ for _ in ()).throw(SourcePageUnreachableError("down")),
    )
    update = make_message_update(111, "https://example.com/schedules")
    context = make_context_with_config(conn)
    context.user_data["new_group_name"] = "Б-Мв-999"

    state = await receive_source_page_url(update, context)

    assert state == ASK_SOURCE_PAGE_URL
    assert storage.count_groups(conn) == 0


@pytest.mark.asyncio
async def test_receive_source_page_url_reports_group_link_not_found(conn, monkeypatch):
    import schedule_bot.group_commands as group_commands_module
    from schedule_bot.group_links import GroupLinkNotFoundError

    monkeypatch.setattr(
        group_commands_module, "resolve_group_pdf_source",
        lambda *a, **k: (_ for _ in ()).throw(GroupLinkNotFoundError("not found")),
    )
    update = make_message_update(111, "https://example.com/schedules")
    context = make_context_with_config(conn)
    context.user_data["new_group_name"] = "Б-Мв-999"

    state = await receive_source_page_url(update, context)

    assert state == ASK_SOURCE_PAGE_URL
    assert storage.count_groups(conn) == 0


@pytest.mark.asyncio
async def test_receive_source_page_url_reports_dead_pdf_link(conn, monkeypatch):
    import schedule_bot.group_commands as group_commands_module

    monkeypatch.setattr(
        group_commands_module, "resolve_group_pdf_source", lambda *a, **k: "https://example.com/dead.pdf"
    )
    monkeypatch.setattr(
        group_commands_module, "_load_pdf_bytes",
        lambda *a, **k: (_ for _ in ()).throw(ConnectionError("404")),
    )
    update = make_message_update(111, "https://example.com/schedules")
    context = make_context_with_config(conn)
    context.user_data["new_group_name"] = "Б-Мв-999"

    state = await receive_source_page_url(update, context)

    assert state == ASK_SOURCE_PAGE_URL
    assert storage.count_groups(conn) == 0


@pytest.mark.asyncio
async def test_receive_source_page_url_creates_group_on_success(conn, monkeypatch):
    import schedule_bot.group_commands as group_commands_module

    monkeypatch.setattr(
        group_commands_module, "resolve_group_pdf_source", lambda *a, **k: "https://example.com/101.pdf"
    )
    monkeypatch.setattr(group_commands_module, "_load_pdf_bytes", lambda *a, **k: b"%PDF-fake")
    update = make_message_update(111, "https://example.com/schedules")
    context = make_context_with_config(conn)
    context.user_data["new_group_name"] = "Б-Мв-999"

    state = await receive_source_page_url(update, context)

    assert state == ConversationHandler.END
    group = storage.get_group_by_name(conn, "Б-Мв-999")
    assert group is not None
    assert group.pdf_source_url == "https://example.com/101.pdf"
    assert storage.get_member(conn, 111).group_id == group.id
    context.bot_data["reschedule_group_jobs"].assert_called_once_with(group.id)


@pytest.mark.asyncio
async def test_receive_source_page_url_handles_integrity_error_race(conn, monkeypatch):
    """Even if the pre-check for an existing name races with a concurrent
    creation, a sqlite3.IntegrityError from storage.create_group itself must
    produce a clean user-facing message, not an unhandled exception."""
    import schedule_bot.group_commands as group_commands_module

    monkeypatch.setattr(
        group_commands_module, "resolve_group_pdf_source", lambda *a, **k: "https://example.com/101.pdf"
    )
    monkeypatch.setattr(group_commands_module, "_load_pdf_bytes", lambda *a, **k: b"%PDF-fake")

    def _raise_integrity_error(*args, **kwargs):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: groups.name")

    monkeypatch.setattr(storage, "create_group", _raise_integrity_error)

    update = make_message_update(111, "https://example.com/schedules")
    context = make_context_with_config(conn)
    context.user_data["new_group_name"] = "Б-Мв-999"

    state = await receive_source_page_url(update, context)

    assert state == ConversationHandler.END
    update.message.reply_text.assert_awaited()


@pytest.mark.asyncio
async def test_cancel_group_creation_ends_conversation(conn):
    update = make_message_update(111, "/cancel")
    context = make_context_with_config(conn)

    state = await cancel_group_creation(update, context)

    assert state == ConversationHandler.END

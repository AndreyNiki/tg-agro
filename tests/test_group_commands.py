import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.group_commands import (
    build_group_picker_keyboard,
    build_institute_picker_keyboard,
    group_request_start_callback,
    institute_request_start_callback,
    validate_and_sync_group_on_first_subscribe,
)

FIXTURE_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_institute(conn, name="ИТ"):
    return storage.create_institute(
        conn, name=name, catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )


def _make_link(conn, institute_id, pdf_url=FIXTURE_PATH, names=("Б-Мв-101",)):
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url=pdf_url, group_names=list(names), raw_label=",".join(names), last_seen_at="t"
    )
    group_ids = [storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name=name) for name in names]
    return link_id, group_ids


def test_build_institute_picker_keyboard_lists_institute_names(conn):
    _make_institute(conn, "Первый")
    _make_institute(conn, "Второй")
    keyboard = build_institute_picker_keyboard(conn)
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "Первый" in texts
    assert "Второй" in texts


def test_build_group_picker_keyboard_hides_unverified_minus_one_groups(conn):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("Good", "Bad"))
    storage.update_group_is_verified(conn, group_ids[1], -1)
    keyboard = build_group_picker_keyboard(conn, institute_id)
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "Good" in texts
    assert "Bad" not in texts


def test_build_institute_picker_keyboard_always_has_missing_institute_button(conn):
    keyboard = build_institute_picker_keyboard(conn)
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert any(b.callback_data == "instreq:new" for b in buttons)


def test_build_group_picker_keyboard_always_has_missing_group_button(conn):
    institute_id = _make_institute(conn)
    keyboard = build_group_picker_keyboard(conn, institute_id)
    buttons = [b for row in keyboard.inline_keyboard for b in row]
    assert any(b.callback_data == f"grpreq:new:{institute_id}" for b in buttons)


@pytest.mark.asyncio
async def test_validate_marks_group_verified_when_column_found(conn):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("Б-Мв-101",))
    group = storage.get_group(conn, group_ids[0])
    notify_admin = AsyncMock()
    sync_fn = AsyncMock(return_value=[])

    ok = await validate_and_sync_group_on_first_subscribe(conn, group, "/tmp/cache", notify_admin, sync_fn)

    assert ok is True
    assert storage.get_group(conn, group.id).is_verified == 1
    sync_fn.assert_awaited_once()


@pytest.mark.asyncio
async def test_validate_marks_group_invalid_and_alerts_admin_when_column_missing(conn):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("НЕТ-ТАКОЙ-ГРУППЫ",))
    group = storage.get_group(conn, group_ids[0])
    notify_admin = AsyncMock()
    sync_fn = AsyncMock(return_value=[])

    ok = await validate_and_sync_group_on_first_subscribe(conn, group, "/tmp/cache", notify_admin, sync_fn)

    assert ok is False
    assert storage.get_group(conn, group.id).is_verified == -1
    notify_admin.assert_awaited_once()
    sync_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_marks_sibling_groups_of_same_link_in_one_pass(conn):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("Б-Мв-101", "НЕТ-ТАКОЙ-ГРУППЫ"))
    good_group = storage.get_group(conn, group_ids[0])
    notify_admin = AsyncMock()
    sync_fn = AsyncMock(return_value=[])

    await validate_and_sync_group_on_first_subscribe(conn, good_group, "/tmp/cache", notify_admin, sync_fn)

    assert storage.get_group(conn, group_ids[0]).is_verified == 1
    assert storage.get_group(conn, group_ids[1]).is_verified == -1


@pytest.mark.asyncio
async def test_validate_is_skipped_for_already_verified_group(conn):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("Б-Мв-101",))
    storage.update_group_is_verified(conn, group_ids[0], 1)
    group = storage.get_group(conn, group_ids[0])
    notify_admin = AsyncMock()
    sync_fn = AsyncMock(return_value=[])

    ok = await validate_and_sync_group_on_first_subscribe(conn, group, "/tmp/cache", notify_admin, sync_fn)

    assert ok is True
    sync_fn.assert_not_awaited()  # already verified -> no re-validation, no forced immediate sync


def _make_select_update(institute_id, group_id, chat_id):
    update = MagicMock()
    query = MagicMock()
    query.answer = AsyncMock()
    query.data = f"grp:select:{institute_id}:{group_id}"
    query.message.reply_text = AsyncMock()
    update.callback_query = query
    update.effective_chat.id = chat_id
    update.effective_user.username = "tester"
    update.effective_user.full_name = "Tester Testovich"
    return update, query


@pytest.mark.asyncio
async def test_first_subscribe_populates_active_version_via_real_sync(conn, tmp_path):
    """Regression for the "first-subscriber sync is a guaranteed no-op" bug:
    sync_institute_link only syncs links whose groups have at least one
    member, so if upsert_member runs AFTER the immediate first sync, that
    sync returns early without even fetching the PDF. Deliberately uses the
    REAL sync_institute_link (not an AsyncMock whose assert_awaited_once
    passes either way) and asserts the observable outcome: an active
    schedule version exists for the group right after subscribing."""
    from schedule_bot.group_commands import group_select_callback
    from schedule_bot.sync import sync_institute_link

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("Б-Мв-101",))
    group_id = group_ids[0]
    chat_id = 4242

    async def real_sync_fn(institute_link_id):
        return await sync_institute_link(
            conn,
            storage.get_institute_link(conn, institute_link_id),
            storage.get_institute(conn, institute_id),
            str(tmp_path),
            AsyncMock(),
            catalog_rescan_state={},
        )

    update, query = _make_select_update(institute_id, group_id, chat_id)
    context = MagicMock()
    context.bot = MagicMock(send_message=AsyncMock(), set_my_commands=AsyncMock())
    context.bot_data = {
        "conn": conn,
        "admin_chat_id": 999,
        "config": MagicMock(pdf_cache_dir=str(tmp_path)),
        "sync_institute_link_fn": real_sync_fn,
    }

    await group_select_callback(update, context)

    assert storage.get_member(conn, chat_id) is not None
    assert storage.get_group(conn, group_id).is_verified == 1
    assert storage.get_active_version(conn, group_id) is not None


@pytest.mark.asyncio
async def test_subscribe_to_group_of_stale_link_makes_no_network_call_and_no_admin_alert(conn, monkeypatch, tmp_path):
    """Binding global constraint: "Устаревшие ссылки никогда не триггерят
    сетевых запросов." Staleness is routine (the admin already got exactly
    one alert when the link vanished from the catalog, spec 1.3), so this
    path must neither fetch nor alert."""
    import schedule_bot.group_commands as group_commands_module
    from schedule_bot.group_commands import group_select_callback

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("Б-Мв-101",))
    storage.mark_stale_links_not_in(conn, institute_id, confirmed_link_ids=[])

    def fail(*args, **kwargs):
        raise AssertionError("stale link must never be fetched")

    monkeypatch.setattr(group_commands_module, "_load_pdf_bytes", fail)

    update, query = _make_select_update(institute_id, group_ids[0], 777)
    sync_fn = AsyncMock(return_value=[])
    context = MagicMock()
    context.bot = MagicMock(send_message=AsyncMock(), set_my_commands=AsyncMock())
    context.bot_data = {
        "conn": conn,
        "admin_chat_id": 999,
        "config": MagicMock(pdf_cache_dir=str(tmp_path)),
        "sync_institute_link_fn": sync_fn,
    }

    await group_select_callback(update, context)

    context.bot.send_message.assert_not_awaited()
    sync_fn.assert_not_awaited()
    assert storage.get_member(conn, 777) is None
    assert storage.get_group(conn, group_ids[0]).is_verified == 0
    assert "недоступна" in query.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_admin_subscribing_to_closed_group_skips_approval(conn):
    """The admin is a super-user: closed groups and prior exclusions
    (join_requests.requires_approval) must never gate the admin — they
    subscribe directly, same as picking an open group."""
    from schedule_bot.group_commands import group_select_callback

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("Б-Мв-101",))
    group_id = group_ids[0]
    storage.update_group_closed(conn, group_id, True)
    admin_chat_id = 999

    update, query = _make_select_update(institute_id, group_id, admin_chat_id)
    context = MagicMock()
    context.bot = MagicMock(send_message=AsyncMock(), set_my_commands=AsyncMock())
    context.bot_data = {
        "conn": conn,
        "admin_chat_id": admin_chat_id,
        "config": MagicMock(pdf_cache_dir="/tmp/cache"),
        "sync_institute_link_fn": AsyncMock(return_value=[]),
    }

    await group_select_callback(update, context)

    member = storage.get_member(conn, admin_chat_id)
    assert member is not None
    assert member.group_id == group_id
    assert storage.get_pending_request_for_chat(conn, admin_chat_id) is None


@pytest.mark.asyncio
async def test_validate_refuses_stale_link_without_fetching(conn, monkeypatch):
    import schedule_bot.group_commands as group_commands_module

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("Б-Мв-101",))
    storage.mark_stale_links_not_in(conn, institute_id, confirmed_link_ids=[])
    group = storage.get_group(conn, group_ids[0])

    monkeypatch.setattr(
        group_commands_module, "_load_pdf_bytes",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch for stale link")),
    )
    notify_admin = AsyncMock()
    sync_fn = AsyncMock(return_value=[])

    ok = await validate_and_sync_group_on_first_subscribe(conn, group, "/tmp/cache", notify_admin, sync_fn)

    assert ok is False
    notify_admin.assert_not_awaited()
    sync_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_rejects_group_found_only_in_lesson_body_text(conn, monkeypatch):
    """Validation must use the SAME column search as the sync pipeline
    (find_header_column_index). With the old whole-table
    find_group_column_index, a name appearing only in lesson body text
    ("совм. с Б-А-301") was accepted (is_verified=1) and then failed every
    subsequent sync forever."""
    import schedule_bot.group_commands as group_commands_module
    from schedule_bot.pdf_parser import Cell, RawRow

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("Б-А-301",))
    group = storage.get_group(conn, group_ids[0])

    rows = [
        RawRow(day_marker=True, time_label="Часы", y0=0, y1=10,
               cells=[Cell("Дни", 0, 30), Cell("Часы", 30, 60), Cell("Б-Мв-101", 60, 300)]),
        RawRow(day_marker=True, time_label="8.30-10.00", y0=10, y1=20,
               cells=[Cell("Пн", 0, 30), Cell("8.30-10.00", 30, 60), Cell("лек. Матан совм. с Б-А-301", 60, 300)]),
    ]
    monkeypatch.setattr(group_commands_module, "_load_pdf_bytes", lambda source: b"pdf")
    monkeypatch.setattr(group_commands_module, "extract_raw_rows", lambda path: (rows, (0, 0, 300, 20)))

    notify_admin = AsyncMock()
    sync_fn = AsyncMock(return_value=[])
    ok = await validate_and_sync_group_on_first_subscribe(conn, group, "/tmp/cache", notify_admin, sync_fn)

    assert ok is False
    assert storage.get_group(conn, group.id).is_verified == -1
    notify_admin.assert_awaited_once()
    sync_fn.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_failure_excludes_group_from_picker_and_does_not_create_membership(conn):
    """Binding review focus: when the first subscriber triggers real-PDF
    validation and the group's name genuinely isn't found in the PDF, all
    four outcomes must hold simultaneously: is_verified == -1, the group is
    excluded from build_group_picker_keyboard, exactly one admin alert
    fires, and no `members` row gets created for the subscribing chat_id.
    This exercises the full group_select_callback path (not just the
    lower-level validate function) so the "no membership row created" half
    of the contract is actually checked end-to-end."""
    from schedule_bot.group_commands import group_select_callback

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link(conn, institute_id, names=("НЕТ-ТАКОЙ-ГРУППЫ",))
    group_id = group_ids[0]
    chat_id = 12345

    update = MagicMock()
    query = MagicMock()
    query.answer = AsyncMock()
    query.data = f"grp:select:{institute_id}:{group_id}"
    query.message.reply_text = AsyncMock()
    update.callback_query = query
    update.effective_chat.id = chat_id
    update.effective_user.username = "tester"
    update.effective_user.full_name = "Tester Testovich"

    sent_messages = []

    async def fake_send_message(chat_id, text):
        sent_messages.append((chat_id, text))

    bot = MagicMock()
    bot.send_message = AsyncMock(side_effect=fake_send_message)
    bot.set_my_commands = AsyncMock()

    context = MagicMock()
    context.bot = bot
    context.bot_data = {
        "conn": conn,
        "admin_chat_id": 999,
        "config": MagicMock(pdf_cache_dir="/tmp/cache"),
        "sync_institute_link_fn": AsyncMock(return_value=[]),
    }

    await group_select_callback(update, context)

    # 1. group ends up marked as not-found-in-PDF.
    assert storage.get_group(conn, group_id).is_verified == -1
    # 2. excluded from the picker shown to users.
    keyboard = build_group_picker_keyboard(conn, institute_id)
    texts = [b.text for row in keyboard.inline_keyboard for b in row]
    assert "НЕТ-ТАКОЙ-ГРУППЫ" not in texts
    # 3. exactly one admin alert was sent.
    assert len(sent_messages) == 1
    assert sent_messages[0][0] == 999
    # 4. no membership row was created for the subscribing chat_id.
    assert storage.get_member(conn, chat_id) is None
    context.bot_data["sync_institute_link_fn"].assert_not_awaited()


@pytest.mark.asyncio
async def test_institute_request_start_callback_stashes_pending_action():
    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    context = MagicMock()
    context.user_data = {}

    await institute_request_start_callback(update, context)

    pending = context.user_data["pending_menu_action"]
    assert pending["action"] == "new_institute_name"
    assert "started_at" in pending
    update.callback_query.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_request_start_callback_stashes_pending_action_with_institute_id():
    update = MagicMock()
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.data = "grpreq:new:42"
    context = MagicMock()
    context.user_data = {}

    await group_request_start_callback(update, context)

    pending = context.user_data["pending_menu_action"]
    assert pending["action"] == "new_group_text"
    assert pending["institute_id"] == 42
    assert "started_at" in pending
    update.callback_query.message.reply_text.assert_awaited_once()

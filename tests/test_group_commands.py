import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.group_commands import (
    build_group_picker_keyboard,
    build_institute_picker_keyboard,
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

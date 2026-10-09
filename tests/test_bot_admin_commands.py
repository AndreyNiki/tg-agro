import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.bot import (
    ADMIN_COMMANDS,
    USER_COMMANDS,
    help_command,
    is_admin,
    set_pdf_source_command,
    set_send_time_command,
    set_check_interval_command,
    refresh_command,
    alert_command,
)

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


def make_context(conn, **extra):
    context = MagicMock()
    context.bot_data = {
        "conn": conn,
        "admin_chat_id": ADMIN_CHAT_ID,
        "reschedule_group_jobs": MagicMock(),
        **extra,
    }
    return context


def make_update(chat_id, args_text=""):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = f"/cmd {args_text}".strip()
    update.message.reply_text = AsyncMock()
    return update


def test_is_admin_true_for_configured_chat_id(conn):
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    assert is_admin(update, context) is True


def test_is_admin_false_for_other_chat_id(conn):
    update = make_update(12345)
    context = make_context(conn)
    assert is_admin(update, context) is False


@pytest.mark.asyncio
async def test_help_command_shows_only_user_commands_for_non_admin(conn):
    update = make_update(12345)
    context = make_context(conn)
    await help_command(update, context)
    text = update.message.reply_text.call_args[0][0]
    for name, _ in USER_COMMANDS:
        assert f"/{name}" in text
    for name, _ in ADMIN_COMMANDS:
        assert f"/{name}" not in text


@pytest.mark.asyncio
async def test_help_command_shows_admin_commands_for_admin(conn):
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    await help_command(update, context)
    text = update.message.reply_text.call_args[0][0]
    for name, _ in USER_COMMANDS + ADMIN_COMMANDS:
        assert f"/{name}" in text


@pytest.mark.asyncio
async def test_set_pdf_source_rejects_non_admin(conn):
    group_id = _make_group(conn)
    update = make_update(12345)
    context = make_context(conn)
    context.args = [str(group_id), "https://example.com/new.pdf"]

    await set_pdf_source_command(update, context)

    assert storage.get_group(conn, group_id).pdf_source_url == "p"


@pytest.mark.asyncio
async def test_set_pdf_source_updates_group_for_admin(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "https://example.com/new.pdf"]

    await set_pdf_source_command(update, context)

    assert storage.get_group(conn, group_id).pdf_source_url == "https://example.com/new.pdf"


@pytest.mark.asyncio
async def test_set_pdf_source_rejects_disallowed_scheme(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "file:///etc/passwd"]

    await set_pdf_source_command(update, context)

    assert storage.get_group(conn, group_id).pdf_source_url == "p"


@pytest.mark.asyncio
async def test_set_pdf_source_accepts_local_path(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "/data/schedule.pdf"]

    await set_pdf_source_command(update, context)

    assert storage.get_group(conn, group_id).pdf_source_url == "/data/schedule.pdf"


@pytest.mark.asyncio
async def test_set_pdf_source_rejects_unknown_group_id(conn):
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = ["9999", "https://example.com/new.pdf"]

    await set_pdf_source_command(update, context)

    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_send_time_reschedules_group_jobs(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "07:30"]

    await set_send_time_command(update, context)

    assert storage.get_group(conn, group_id).send_time == "07:30"
    context.bot_data["reschedule_group_jobs"].assert_called_once_with(group_id)


@pytest.mark.asyncio
async def test_set_check_interval_reschedules_group_jobs(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "30"]

    await set_check_interval_command(update, context)

    assert storage.get_group(conn, group_id).check_interval_minutes == 30
    context.bot_data["reschedule_group_jobs"].assert_called_once_with(group_id)


@pytest.mark.asyncio
async def test_set_send_time_rejects_malformed_value_without_writing(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "9h30"]

    await set_send_time_command(update, context)

    assert storage.get_group(conn, group_id).send_time == "09:00"
    context.bot_data["reschedule_group_jobs"].assert_not_called()


@pytest.mark.asyncio
async def test_set_check_interval_rejects_non_numeric_value_without_writing(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "abc"]

    await set_check_interval_command(update, context)

    assert storage.get_group(conn, group_id).check_interval_minutes == 60
    context.bot_data["reschedule_group_jobs"].assert_not_called()


@pytest.mark.asyncio
async def test_refresh_command_reports_result(conn):
    group_id = _make_group(conn)

    async def fake_sync(group_id_arg):
        from schedule_bot.sync import SyncResult
        return SyncResult(status="no_changes")

    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn, sync_fn=fake_sync)
    context.args = [str(group_id)]

    await refresh_command(update, context)

    assert "Изменений нет" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_refresh_command_reports_sync_error_without_crashing(conn):
    group_id = _make_group(conn)

    async def failing_sync(group_id_arg):
        raise ConnectionError("network unreachable")

    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn, sync_fn=failing_sync)
    context.args = [str(group_id)]

    await refresh_command(update, context)

    assert "не удал" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_alert_command_broadcasts_to_every_group_globally(conn):
    """/alert is deliberately global (bot-update announcements), not
    per-group — members of two DIFFERENT groups must both receive it."""
    group_a = _make_group(conn, "A")
    group_b = _make_group(conn, "B")
    storage.upsert_member(conn, 111, group_a, joined_at="t")
    storage.upsert_member(conn, 222, group_b, joined_at="t")
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = ["Внимание,", "вышло", "обновление", "бота"]
    context.bot.send_message = AsyncMock()

    await alert_command(update, context)

    assert context.bot.send_message.await_count == 2
    sent_chat_ids = {call.kwargs["chat_id"] for call in context.bot.send_message.await_args_list}
    assert sent_chat_ids == {111, 222}


@pytest.mark.asyncio
async def test_alert_command_rejects_non_admin(conn):
    storage.upsert_member(conn, 111, _make_group(conn), joined_at="t")
    update = make_update(12345)
    context = make_context(conn)
    context.args = ["test"]
    context.bot.send_message = AsyncMock()

    await alert_command(update, context)

    context.bot.send_message.assert_not_awaited()


from datetime import datetime, timezone

from schedule_bot.bot import remove_starosta_command, set_starosta_command


@pytest.mark.asyncio
async def test_set_starosta_assigns_and_notifies(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "111"]
    context.bot.send_message = AsyncMock()

    await set_starosta_command(update, context)

    assert storage.get_group(conn, group_id).starosta_chat_id == 111
    assert storage.get_group(conn, group_id).last_starosta_change_at is not None
    assert context.bot.send_message.await_args.kwargs["chat_id"] == 111


@pytest.mark.asyncio
async def test_set_starosta_cancels_open_election_and_resets_cooldown(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.set_group_last_starosta_change_at(conn, group_id, "2026-10-01T00:00:00+00:00")
    election_id = storage.create_election(conn, group_id, "change", 222, 333, "t")
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "111"]
    context.bot.send_message = AsyncMock()

    await set_starosta_command(update, context)

    assert storage.get_election(conn, election_id).status == "cancelled"
    assert storage.get_group(conn, group_id).last_starosta_change_at != "2026-10-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_set_starosta_rejects_non_admin(conn):
    group_id = _make_group(conn)
    update = make_update(12345)
    context = make_context(conn)
    context.args = [str(group_id), "111"]
    context.bot.send_message = AsyncMock()

    await set_starosta_command(update, context)

    assert storage.get_group(conn, group_id).starosta_chat_id is None
    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_starosta_rejects_non_numeric_chat_id(conn):
    group_id = _make_group(conn)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "not-a-number"]

    await set_starosta_command(update, context)

    assert storage.get_group(conn, group_id).starosta_chat_id is None


@pytest.mark.asyncio
async def test_remove_starosta_clears_and_resets_cooldown(conn):
    group_id = _make_group(conn)
    storage.update_group_starosta(conn, group_id, 111)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id)]

    await remove_starosta_command(update, context)

    group = storage.get_group(conn, group_id)
    assert group.starosta_chat_id is None
    assert group.last_starosta_change_at is not None


@pytest.mark.asyncio
async def test_remove_starosta_rejects_non_admin(conn):
    group_id = _make_group(conn)
    storage.update_group_starosta(conn, group_id, 111)
    update = make_update(12345)
    context = make_context(conn)
    context.args = [str(group_id)]

    await remove_starosta_command(update, context)

    assert storage.get_group(conn, group_id).starosta_chat_id == 111


from schedule_bot.bot import close_group_command, notify_command, open_group_command


@pytest.mark.asyncio
async def test_close_group_by_starosta_without_group_id(conn):
    group_id = _make_group(conn)
    storage.update_group_starosta(conn, group_id, 111)
    update = make_update(111)
    context = make_context(conn)
    context.args = []

    await close_group_command(update, context)

    assert storage.get_group(conn, group_id).is_closed is True


@pytest.mark.asyncio
async def test_close_group_rejects_plain_member(conn):
    group_id = _make_group(conn)
    update = make_update(111)
    context = make_context(conn)
    context.args = []

    await close_group_command(update, context)

    assert storage.get_group(conn, group_id).is_closed is False
    assert "не староста" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_open_group_by_admin_with_group_id(conn):
    group_id = _make_group(conn)
    storage.update_group_closed(conn, group_id, True)
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id)]

    await open_group_command(update, context)

    assert storage.get_group(conn, group_id).is_closed is False


@pytest.mark.asyncio
async def test_open_group_admin_without_group_id_asks_for_usage(conn):
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = []

    await open_group_command(update, context)

    assert "Использование" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_notify_command_by_starosta_broadcasts_to_whole_group(conn):
    group_id = _make_group(conn)
    storage.update_group_starosta(conn, group_id, 111)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t", subscribed=False)
    update = make_update(111)
    context = make_context(conn)
    context.args = ["Внимание"]
    context.bot.send_message = AsyncMock()

    await notify_command(update, context)

    assert context.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_notify_command_rejects_plain_member(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_update(111)
    context = make_context(conn)
    context.args = ["Внимание"]
    context.bot.send_message = AsyncMock()

    await notify_command(update, context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_command_by_admin_requires_group_id(conn):
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = ["текст", "без", "group_id"]
    context.bot.send_message = AsyncMock()

    await notify_command(update, context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_command_by_admin_with_group_id_broadcasts(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    context.args = [str(group_id), "Внимание"]
    context.bot.send_message = AsyncMock()

    await notify_command(update, context)

    context.bot.send_message.assert_awaited_once()

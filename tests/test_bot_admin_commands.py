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
    set_group_command,
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


def make_context(conn, **extra):
    context = MagicMock()
    context.bot_data = {
        "conn": conn,
        "admin_chat_id": ADMIN_CHAT_ID,
        "group_name": "Б-Мв-101",
        "reschedule_daily_job": MagicMock(),
        "reschedule_hourly_job": MagicMock(),
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
    update = make_update(12345, "https://example.com/new.pdf")
    context = make_context(conn)
    context.args = ["https://example.com/new.pdf"]

    await set_pdf_source_command(update, context)

    update.message.reply_text.assert_awaited_once()
    assert storage.get_setting(conn, "pdf_source") is None


@pytest.mark.asyncio
async def test_set_pdf_source_updates_setting_for_admin(conn):
    update = make_update(ADMIN_CHAT_ID, "https://example.com/new.pdf")
    context = make_context(conn)
    context.args = ["https://example.com/new.pdf"]

    await set_pdf_source_command(update, context)

    assert storage.get_setting(conn, "pdf_source") == "https://example.com/new.pdf"


@pytest.mark.asyncio
async def test_set_send_time_reschedules_daily_job(conn):
    update = make_update(ADMIN_CHAT_ID, "07:30")
    context = make_context(conn)
    context.args = ["07:30"]

    await set_send_time_command(update, context)

    assert storage.get_setting(conn, "daily_send_time") == "07:30"
    context.bot_data["reschedule_daily_job"].assert_called_once()


@pytest.mark.asyncio
async def test_set_check_interval_reschedules_hourly_job(conn):
    update = make_update(ADMIN_CHAT_ID, "30")
    context = make_context(conn)
    context.args = ["30"]

    await set_check_interval_command(update, context)

    assert storage.get_setting(conn, "check_interval_minutes") == "30"
    context.bot_data["reschedule_hourly_job"].assert_called_once()


@pytest.mark.asyncio
async def test_set_send_time_rejects_malformed_value_without_writing(conn):
    """C2 regression: malformed /set_send_time input must not be persisted."""
    update = make_update(ADMIN_CHAT_ID, "9h30")
    context = make_context(conn)
    context.args = ["9h30"]

    await set_send_time_command(update, context)

    assert storage.get_setting(conn, "daily_send_time") is None
    context.bot_data["reschedule_daily_job"].assert_not_called()
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_send_time_rejects_out_of_range_value(conn):
    update = make_update(ADMIN_CHAT_ID, "25:00")
    context = make_context(conn)
    context.args = ["25:00"]

    await set_send_time_command(update, context)

    assert storage.get_setting(conn, "daily_send_time") is None


@pytest.mark.asyncio
async def test_set_check_interval_rejects_non_numeric_value_without_writing(conn):
    """C2 regression: malformed /set_check_interval input must not be persisted."""
    update = make_update(ADMIN_CHAT_ID, "abc")
    context = make_context(conn)
    context.args = ["abc"]

    await set_check_interval_command(update, context)

    assert storage.get_setting(conn, "check_interval_minutes") is None
    context.bot_data["reschedule_hourly_job"].assert_not_called()
    update.message.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_set_check_interval_rejects_non_positive_value(conn):
    update = make_update(ADMIN_CHAT_ID, "0")
    context = make_context(conn)
    context.args = ["0"]

    await set_check_interval_command(update, context)

    assert storage.get_setting(conn, "check_interval_minutes") is None


@pytest.mark.asyncio
async def test_refresh_command_reports_result(conn):
    async def fake_sync(*args, **kwargs):
        from schedule_bot.sync import SyncResult

        return SyncResult(status="no_changes")

    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn, sync_fn=fake_sync)

    await refresh_command(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "Изменений нет" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_refresh_command_reports_sync_error_without_crashing(conn):
    async def failing_sync(*args, **kwargs):
        raise ConnectionError("network unreachable")

    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn, sync_fn=failing_sync)

    await refresh_command(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "не удал" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_alert_command_broadcasts_to_all_subscribers(conn):
    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    storage.add_subscriber(conn, 222, "Б-Мв-101", "2026-09-16T00:00:00")
    update = make_update(ADMIN_CHAT_ID, "Внимание, ошибка в расписании")
    context = make_context(conn)
    context.args = ["Внимание,", "ошибка", "в", "расписании"]
    context.bot.send_message = AsyncMock()

    await alert_command(update, context)

    assert context.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_alert_command_rejects_non_admin(conn):
    update = make_update(12345, "test")
    context = make_context(conn)
    context.args = ["test"]
    context.bot.send_message = AsyncMock()

    await alert_command(update, context)

    context.bot.send_message.assert_not_awaited()

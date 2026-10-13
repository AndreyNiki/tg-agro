import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from telegram import BotCommandScopeChat

from schedule_bot import storage
from schedule_bot.bot import (
    ADMIN_COMMANDS,
    BASE_VISIBLE_COMMANDS,
    MEMBER_VISIBLE_COMMANDS,
    USER_COMMANDS,
    help_command,
    is_admin,
    refresh_command,
    alert_command,
    sync_visible_commands,
)

ADMIN_CHAT_ID = 999


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


def _make_link_and_group(conn, institute_id, name="Б-Мв-101", pdf_url="https://example.com/schedule.pdf"):
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url=pdf_url, group_names=[name], raw_label=name, last_seen_at="t",
    )
    group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name=name)
    return link_id, group_id


_group_counter = [0]


def _make_group(conn, name="Б-Мв-101"):
    _group_counter[0] += 1
    institute_id = _make_institute(conn, name=f"ИТ-{_group_counter[0]}")
    _, group_id = _make_link_and_group(conn, institute_id, name=name)
    return group_id


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
async def test_sync_visible_commands_sets_base_list_for_non_member(conn):
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()

    await sync_visible_commands(bot, conn, 12345)

    bot.set_my_commands.assert_awaited_once()
    names = [c.command for c in bot.set_my_commands.await_args.args[0]]
    assert names == [name for name, _ in BASE_VISIBLE_COMMANDS]
    assert bot.set_my_commands.await_args.kwargs["scope"] == BotCommandScopeChat(chat_id=12345)


@pytest.mark.asyncio
async def test_sync_visible_commands_adds_schedule_for_member(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 777, group_id, joined_at="t")
    bot = MagicMock()
    bot.set_my_commands = AsyncMock()

    await sync_visible_commands(bot, conn, 777)

    names = [c.command for c in bot.set_my_commands.await_args.args[0]]
    assert names == [name for name, _ in MEMBER_VISIBLE_COMMANDS]


def test_requests_registered_in_admin_commands():
    # Regression: /requests was wired as a handler but missing from
    # ADMIN_COMMANDS, making it invisible in /help and the command menu.
    assert any(name == "requests" for name, _ in ADMIN_COMMANDS)


@pytest.mark.asyncio
async def test_refresh_command_resolves_institute_link_and_calls_sync_fn(conn):
    institute_id = _make_institute(conn)
    link_id, group_id = _make_link_and_group(conn, institute_id)
    sync_calls = []

    async def fake_sync_institute_link_fn(institute_link_id):
        sync_calls.append(institute_link_id)
        from schedule_bot.sync import SyncResult
        return [SyncResult(group_id=group_id, status="no_changes")]

    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn, sync_institute_link_fn=fake_sync_institute_link_fn)
    context.args = [str(group_id)]

    await refresh_command(update, context)

    assert sync_calls == [link_id]
    assert "Изменений нет" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_refresh_command_reports_sync_error_without_crashing(conn):
    group_id = _make_group(conn)

    async def failing_sync(institute_link_id):
        raise ConnectionError("network unreachable")

    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn, sync_institute_link_fn=failing_sync)
    context.args = [str(group_id)]

    await refresh_command(update, context)

    assert "не удал" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_refresh_command_reports_own_group_result_when_other_groups_also_synced(conn):
    """The institute link may be shared by several groups — /refresh for
    one group_id must report THAT group's own result, not just the first
    result in the list."""
    institute_id = _make_institute(conn)
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url="https://example.com/s.pdf",
        group_names=["A", "B"], raw_label="A,B", last_seen_at="t",
    )
    group_a = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="A")
    group_b = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="B")

    async def fake_sync_institute_link_fn(institute_link_id):
        from schedule_bot.sync import SyncResult
        return [
            SyncResult(group_id=group_a, status="changed"),
            SyncResult(group_id=group_b, status="no_changes"),
        ]

    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn, sync_institute_link_fn=fake_sync_institute_link_fn)
    context.args = [str(group_b)]

    await refresh_command(update, context)

    assert "Изменений нет" in update.message.reply_text.call_args[0][0]


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


from schedule_bot.bot import (
    _apply_alert,
    _apply_notify,
    _apply_remove_starosta,
    _apply_set_starosta,
)


@pytest.mark.asyncio
async def test_apply_set_starosta_assigns_and_notifies(conn):
    group_id = _make_group(conn)
    group = storage.get_group(conn, group_id)
    bot_client = MagicMock()
    bot_client.send_message = AsyncMock()

    text = await _apply_set_starosta(conn, bot_client, group, "111")

    assert "назначен" in text
    assert storage.get_group(conn, group_id).starosta_chat_id == 111
    bot_client.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_remove_starosta_clears(conn):
    group_id = _make_group(conn)
    storage.update_group_starosta(conn, group_id, 111)
    group = storage.get_group(conn, group_id)

    text = _apply_remove_starosta(conn, group)

    assert "снят" in text
    assert storage.get_group(conn, group_id).starosta_chat_id is None


@pytest.mark.asyncio
async def test_apply_notify_broadcasts_to_group(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    group = storage.get_group(conn, group_id)
    bot_client = MagicMock()
    bot_client.send_message = AsyncMock()

    text = await _apply_notify(conn, bot_client, group, "Внимание")

    assert "1" in text
    bot_client.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_alert_broadcasts_globally(conn):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    bot_client = MagicMock()
    bot_client.send_message = AsyncMock()

    text = await _apply_alert(conn, bot_client, "Обновление бота")

    assert "1" in text
    bot_client.send_message.assert_awaited_once()

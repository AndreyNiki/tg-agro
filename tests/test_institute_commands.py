import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.institute_commands import (
    add_institute_command,
    institute_links_command,
    refresh_institute_links_command,
    set_institute_catalog_url_command,
    set_institute_check_interval_command,
)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _update(args, chat_id=999):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.reply_text = AsyncMock()
    update.message.text = " ".join(args)
    return update


def _context(conn, args, admin_chat_id=999):
    context = MagicMock()
    context.args = args
    context.bot_data = {
        "conn": conn, "admin_chat_id": admin_chat_id,
        "config": MagicMock(
            default_daily_send_time="09:00", default_check_interval_minutes=60,
            default_timezone="Europe/Moscow", default_catalog_refresh_interval_minutes=10080,
        ),
        "reschedule_institute_jobs": MagicMock(),
        "reschedule_institute_link_jobs": MagicMock(),
        "_catalog_locks": {},
    }
    return context


@pytest.mark.asyncio
async def test_add_institute_creates_row_and_reschedules(conn):
    update = _update(["ИТ", "https://example.com/catalog"])
    context = _context(conn, ["ИТ", "https://example.com/catalog"])

    await add_institute_command(update, context)

    institutes = storage.list_institutes(conn)
    assert len(institutes) == 1
    assert institutes[0].name == "ИТ"
    context.bot_data["reschedule_institute_jobs"].assert_called_once()


@pytest.mark.asyncio
async def test_add_institute_rejects_non_admin(conn):
    update = _update(["ИТ", "u"], chat_id=1)
    context = _context(conn, ["ИТ", "u"], admin_chat_id=999)

    await add_institute_command(update, context)

    assert storage.list_institutes(conn) == []
    update.message.reply_text.assert_awaited_once_with("Недостаточно прав.")


@pytest.mark.asyncio
async def test_set_institute_catalog_url_updates_and_reports(conn):
    institute_id = storage.create_institute(
        conn, name="ИТ", catalog_url="old", check_interval_minutes=60, send_time="09:00",
        tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )
    update = _update([str(institute_id), "https://new.example.com"])
    context = _context(conn, [str(institute_id), "https://new.example.com"])

    await set_institute_catalog_url_command(update, context)

    assert storage.get_institute(conn, institute_id).catalog_url == "https://new.example.com"


@pytest.mark.asyncio
async def test_set_institute_check_interval_rejects_non_positive(conn):
    institute_id = storage.create_institute(
        conn, name="ИТ", catalog_url="u", check_interval_minutes=60, send_time="09:00",
        tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )
    update = _update([str(institute_id), "0"])
    context = _context(conn, [str(institute_id), "0"])

    await set_institute_check_interval_command(update, context)

    assert storage.get_institute(conn, institute_id).check_interval_minutes == 60
    update.message.reply_text.assert_awaited_once()
    assert "Неверное" in update.message.reply_text.await_args.args[0]


@pytest.mark.asyncio
async def test_refresh_institute_links_command_invokes_catalog_scan(conn, monkeypatch):
    import schedule_bot.institute_commands as institute_commands_module

    institute_id = storage.create_institute(
        conn, name="ИТ", catalog_url="https://example.com/catalog", check_interval_minutes=60,
        send_time="09:00", tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )
    called = []
    monkeypatch.setattr(institute_commands_module, "refresh_institute_catalog", lambda conn, institute, **kw: called.append(institute.id))

    update = _update([str(institute_id)])
    context = _context(conn, [str(institute_id)])

    await refresh_institute_links_command(update, context)

    assert called == [institute_id]


@pytest.mark.asyncio
async def test_institute_links_command_lists_raw_labels_and_group_counts(conn):
    institute_id = storage.create_institute(
        conn, name="ИТ", catalog_url="u", check_interval_minutes=60, send_time="09:00",
        tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )
    storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url="a.pdf", group_names=["A", "B"], raw_label="A,B", last_seen_at="t"
    )
    storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url="b.pdf", group_names=[], raw_label="1 курс БЭ, БМ оз", last_seen_at="t"
    )
    update = _update([str(institute_id)])
    context = _context(conn, [str(institute_id)])

    await institute_links_command(update, context)

    text = update.message.reply_text.await_args.args[0]
    assert "A,B" in text
    assert "2" in text  # group count for the first link
    assert "1 курс БЭ, БМ оз" in text
    assert "0" in text  # group count for the second link


@pytest.mark.asyncio
async def test_add_institute_reports_zero_links_honestly_and_forwards_alert(conn, monkeypatch):
    """A 0-link scan must not be reported as a bare success, and the
    rescan's own admin alert must not be swallowed by a no-op lambda."""
    import schedule_bot.institute_commands as institute_commands_module

    def fake_refresh(conn_, institute, *, notify_admin):
        notify_admin(f"Каталог института {institute.name} (id={institute.id}) вернул 0 ссылок")

    monkeypatch.setattr(institute_commands_module, "refresh_institute_catalog", fake_refresh)

    update = _update(["ИТ", "https://example.com/catalog"])
    context = _context(conn, ["ИТ", "https://example.com/catalog"])

    await add_institute_command(update, context)

    texts = [call.args[0] for call in update.message.reply_text.await_args_list]
    assert any("вернул 0 ссылок" in text for text in texts)
    assert any("ссылок не найдено" in text for text in texts)


@pytest.mark.asyncio
async def test_add_institute_runs_blocking_catalog_fetch_off_the_event_loop(conn, monkeypatch):
    import threading

    import schedule_bot.institute_commands as institute_commands_module

    seen_threads = []
    monkeypatch.setattr(
        institute_commands_module, "refresh_institute_catalog",
        lambda *a, **k: seen_threads.append(threading.current_thread()),
    )

    update = _update(["ИТ", "https://example.com/catalog"])
    context = _context(conn, ["ИТ", "https://example.com/catalog"])
    await add_institute_command(update, context)

    assert seen_threads and seen_threads[0] is not threading.main_thread()


@pytest.mark.asyncio
async def test_refresh_institute_links_runs_blocking_catalog_fetch_off_the_event_loop(conn, monkeypatch):
    import threading

    import schedule_bot.institute_commands as institute_commands_module

    institute_id = storage.create_institute(
        conn, name="ИТ", catalog_url="https://example.com/catalog", check_interval_minutes=60,
        send_time="09:00", tz_name="Europe/Moscow", catalog_refresh_interval_minutes=10080,
    )
    seen_threads = []
    monkeypatch.setattr(
        institute_commands_module, "refresh_institute_catalog",
        lambda *a, **k: seen_threads.append(threading.current_thread()),
    )

    update = _update([str(institute_id)])
    context = _context(conn, [str(institute_id)])
    await refresh_institute_links_command(update, context)

    assert seen_threads and seen_threads[0] is not threading.main_thread()


@pytest.mark.asyncio
async def test_add_institute_command_accepts_multi_word_name(conn, monkeypatch):
    monkeypatch.setattr(
        "schedule_bot.institute_commands.refresh_institute_catalog", lambda conn_, institute, notify_admin: None
    )
    update = _update(["Аграрный", "институт", "https://example.com/catalog"])
    context = _context(conn, ["Аграрный", "институт", "https://example.com/catalog"])

    await add_institute_command(update, context)

    institutes = storage.list_institutes(conn)
    assert len(institutes) == 1
    assert institutes[0].name == "Аграрный институт"
    assert institutes[0].catalog_url == "https://example.com/catalog"


@pytest.mark.asyncio
async def test_add_institute_command_rejects_url_not_starting_with_http(conn):
    update = _update(["Институт", "not-a-url"])
    context = _context(conn, ["Институт", "not-a-url"])

    await add_institute_command(update, context)

    assert storage.list_institutes(conn) == []
    update.message.reply_text.assert_awaited_with(
        "Использование: /add_institute <название> <url_каталога> — url должен начинаться с http:// или https://"
    )

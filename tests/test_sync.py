import asyncio
import sqlite3
from pathlib import Path

import pytest

from schedule_bot import storage
from schedule_bot.sync import check_and_apply_diff

FIXTURE_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


@pytest.fixture
def conn(tmp_path):
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_group(conn, pdf_source_url=FIXTURE_PATH, source_page_url="https://example.com/schedules"):
    return storage.create_group(
        conn,
        name="Б-Мв-101",
        source_page_url=source_page_url,
        pdf_source_url=pdf_source_url,
        send_time="09:00",
        check_interval_minutes=60,
        tz_name="Europe/Moscow",
    )


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_first_sync_seeds_active_version_without_notifying(conn, tmp_path):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="2026-09-16T00:00:00")
    notifier = RecordingNotifier()

    group = storage.get_group(conn, group_id)
    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "initial"
    assert notifier.sent == []
    assert storage.get_active_version(conn, group_id) is not None


def test_second_sync_with_same_pdf_reports_no_changes(conn, tmp_path):
    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "no_changes"
    assert notifier.sent == []


def test_sync_with_changed_pdf_notifies_all_subscribers(conn, tmp_path, monkeypatch):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    import schedule_bot.sync as sync_module
    from schedule_bot.pdf_parser import parse_schedule as real_parse_schedule

    def fake_parse_schedule(source, group_name):
        schedule = real_parse_schedule(source, group_name)
        schedule.days["Среда"].lessons[0].upper = "ИЗМЕНЁННЫЙ ПРЕДМЕТ"
        return schedule

    monkeypatch.setattr(sync_module, "parse_schedule", fake_parse_schedule)

    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "applied"
    assert len(result.changes) >= 1
    assert {chat_id for chat_id, _ in notifier.sent} == {111, 222}


def test_write_cache_sanitizes_group_name_used_as_path_segment(tmp_path):
    from schedule_bot.sync import _write_cache

    cache_path = Path(_write_cache(b"%PDF-fake", str(tmp_path), "Б-Мв/../101"))

    assert cache_path.exists()
    assert tmp_path in cache_path.resolve().parents
    assert ".." not in cache_path.parts


def test_sync_caches_pdf_file_on_disk(conn, tmp_path):
    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))
    active = storage.get_active_version(conn, group_id)
    assert Path(active.pdf_cache_path).exists()


class PartiallyFailingNotifier:
    def __init__(self, failing_chat_id):
        self.failing_chat_id = failing_chat_id
        self.sent = []

    async def __call__(self, chat_id, text):
        if chat_id == self.failing_chat_id:
            raise RuntimeError("bot blocked by user")
        self.sent.append((chat_id, text))


def test_sync_reads_pdf_source_exactly_once_and_cache_matches_parsed_bytes(conn, tmp_path, monkeypatch):
    import schedule_bot.pdf_parser as pdf_parser_module
    import schedule_bot.sync as sync_module

    real_load_pdf_bytes = pdf_parser_module._load_pdf_bytes
    call_count = 0

    def counting_load_pdf_bytes(source):
        nonlocal call_count
        call_count += 1
        return real_load_pdf_bytes(source)

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", counting_load_pdf_bytes)

    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "initial"
    assert call_count == 1

    active = storage.get_active_version(conn, group_id)
    assert Path(active.pdf_cache_path).read_bytes() == Path(FIXTURE_PATH).read_bytes()


def test_sync_deletes_superseded_cache_file_on_change(conn, tmp_path, monkeypatch):
    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))
    first_cache_path = Path(storage.get_active_version(conn, group_id).pdf_cache_path)
    assert first_cache_path.exists()

    import schedule_bot.sync as sync_module
    from schedule_bot.pdf_parser import parse_schedule as real_parse_schedule

    def fake_parse_schedule(source, group_name):
        schedule = real_parse_schedule(source, group_name)
        schedule.days["Среда"].lessons[0].upper = "ИЗМЕНЁННЫЙ ПРЕДМЕТ"
        return schedule

    monkeypatch.setattr(sync_module, "parse_schedule", fake_parse_schedule)

    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "applied"
    second_cache_path = Path(storage.get_active_version(conn, group_id).pdf_cache_path)
    assert not first_cache_path.exists()
    assert second_cache_path.exists()
    assert second_cache_path != first_cache_path


def test_sync_retries_transient_fetch_failure_and_succeeds(conn, tmp_path, monkeypatch):
    import schedule_bot.pdf_parser as pdf_parser_module
    import schedule_bot.sync as sync_module

    real_load_pdf_bytes = pdf_parser_module._load_pdf_bytes
    call_count = 0

    def flaky_load_pdf_bytes(source):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise ConnectionError("transient network error")
        return real_load_pdf_bytes(source)

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", flaky_load_pdf_bytes)
    # Disable 404-reconnect for this test: it's about plain transient
    # retries, not link rediscovery, and resolve_group_pdf_source would
    # otherwise fire a real network call on the first failure.
    monkeypatch.setattr(sync_module, "resolve_group_pdf_source", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no reconnect")))

    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "initial"
    assert call_count == 3
    assert storage.get_group(conn, group_id).last_sync_failed_at in (None, "")


def test_sync_records_failure_after_exhausting_retries(conn, tmp_path, monkeypatch):
    import schedule_bot.sync as sync_module

    def always_fails(source):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", always_fails)
    monkeypatch.setattr(sync_module, "resolve_group_pdf_source", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("source page also down")))

    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    with pytest.raises(ConnectionError):
        asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert storage.get_group(conn, group_id).last_sync_failed_at


def test_sync_clears_failure_flag_on_next_successful_sync(conn, tmp_path, monkeypatch):
    import schedule_bot.sync as sync_module

    def always_fails(source):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", always_fails)
    monkeypatch.setattr(sync_module, "resolve_group_pdf_source", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no reconnect")))

    group_id = _make_group(conn)
    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    with pytest.raises(ConnectionError):
        asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))
    assert storage.get_group(conn, group_id).last_sync_failed_at

    monkeypatch.undo()

    group = storage.get_group(conn, group_id)
    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "initial"
    assert storage.get_group(conn, group_id).last_sync_failed_at in (None, "")


def test_sync_notifies_remaining_subscribers_when_one_notify_fails(conn, tmp_path, monkeypatch):
    group_id = _make_group(conn)
    storage.upsert_member(conn, 111, group_id, joined_at="t")
    storage.upsert_member(conn, 222, group_id, joined_at="t")
    seed_notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), seed_notifier))

    import schedule_bot.sync as sync_module
    from schedule_bot.pdf_parser import parse_schedule as real_parse_schedule

    def fake_parse_schedule(source, group_name):
        schedule = real_parse_schedule(source, group_name)
        schedule.days["Среда"].lessons[0].upper = "ИЗМЕНЁННЫЙ ПРЕДМЕТ"
        return schedule

    monkeypatch.setattr(sync_module, "parse_schedule", fake_parse_schedule)

    notifier = PartiallyFailingNotifier(failing_chat_id=111)
    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "applied"
    assert {chat_id for chat_id, _ in notifier.sent} == {222}


def test_sync_reconnects_on_404_and_persists_new_pdf_source_url(conn, tmp_path, monkeypatch):
    """The motivating bug fix: a dead pdf_source_url must trigger one
    lookup of a fresh link on source_page_url, and the group's stored
    pdf_source_url must be updated to the rediscovered link on success."""
    import schedule_bot.sync as sync_module

    group_id = _make_group(conn, pdf_source_url="https://example.com/dead-link.pdf")

    def fake_load_pdf_bytes(source):
        if source == "https://example.com/dead-link.pdf":
            raise ConnectionError("404")
        return Path(FIXTURE_PATH).read_bytes()

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", fake_load_pdf_bytes)
    monkeypatch.setattr(
        sync_module, "resolve_group_pdf_source",
        lambda source_page_url, group_name: "https://example.com/fresh-link.pdf",
    )

    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    result = asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert result.status == "initial"
    assert storage.get_group(conn, group_id).pdf_source_url == "https://example.com/fresh-link.pdf"


def test_sync_reconnect_attempted_only_once_per_sync_call(conn, tmp_path, monkeypatch):
    """A dead link whose rediscovered replacement is ALSO dead must not
    cause repeated reconnect attempts — only one lookup per sync call,
    the remaining retry budget just keeps failing against the same
    (still dead) resolved link."""
    import schedule_bot.sync as sync_module

    group_id = _make_group(conn, pdf_source_url="https://example.com/dead-link.pdf")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", lambda source: (_ for _ in ()).throw(ConnectionError("404")))
    reconnect_calls = []

    def fake_resolve(source_page_url, group_name):
        reconnect_calls.append((source_page_url, group_name))
        return "https://example.com/also-dead.pdf"

    monkeypatch.setattr(sync_module, "resolve_group_pdf_source", fake_resolve)

    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    with pytest.raises(ConnectionError):
        asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

    assert len(reconnect_calls) == 1


def test_sync_reconnect_failure_falls_back_to_original_error(conn, tmp_path, monkeypatch):
    """If the source page itself is unreachable (or has no matching link),
    the sync must still fail with the original fetch error within the
    existing retry budget, not hang or raise something new."""
    import schedule_bot.sync as sync_module
    from schedule_bot.group_links import SourcePageUnreachableError

    group_id = _make_group(conn, pdf_source_url="https://example.com/dead-link.pdf")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", lambda source: (_ for _ in ()).throw(ConnectionError("404")))
    monkeypatch.setattr(
        sync_module, "resolve_group_pdf_source",
        lambda *a, **k: (_ for _ in ()).throw(SourcePageUnreachableError("page down")),
    )

    notifier = RecordingNotifier()
    group = storage.get_group(conn, group_id)
    with pytest.raises(ConnectionError):
        asyncio.run(check_and_apply_diff(conn, group, str(tmp_path), notifier))

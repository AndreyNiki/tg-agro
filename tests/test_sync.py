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


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_second_sync_with_same_pdf_reports_no_changes(conn, tmp_path):
    notifier = RecordingNotifier()
    asyncio.run(check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier))

    result = asyncio.run(
        check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier)
    )

    assert result.status == "no_changes"
    assert notifier.sent == []


def test_write_cache_sanitizes_group_name_used_as_path_segment(tmp_path):
    """group_name ends up as a path segment under pdf_cache_dir; a stray
    "/" or ".." must not be interpreted as a path separator/traversal."""
    from schedule_bot.sync import _write_cache

    cache_path = Path(_write_cache(b"%PDF-fake", str(tmp_path), "Б-Мв/../101"))

    assert cache_path.exists()
    assert tmp_path in cache_path.resolve().parents
    assert ".." not in cache_path.parts


def test_sync_caches_pdf_file_on_disk(conn, tmp_path):
    notifier = RecordingNotifier()
    asyncio.run(check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier))
    active = storage.get_active_version(conn, "Б-Мв-101")
    assert Path(active.pdf_cache_path).exists()


def test_sync_reads_pdf_source_exactly_once_and_cache_matches_parsed_bytes(conn, tmp_path, monkeypatch):
    """I5: pdf_source must be read exactly once per sync call, and the bytes
    written to the cache file must be identical to the bytes that were parsed."""
    import schedule_bot.pdf_parser as pdf_parser_module
    import schedule_bot.sync as sync_module

    real_load_pdf_bytes = pdf_parser_module._load_pdf_bytes
    call_count = 0

    def counting_load_pdf_bytes(source):
        nonlocal call_count
        call_count += 1
        return real_load_pdf_bytes(source)

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", counting_load_pdf_bytes)

    notifier = RecordingNotifier()
    result = asyncio.run(
        check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier)
    )

    assert result.status == "initial"
    assert call_count == 1

    active = storage.get_active_version(conn, "Б-Мв-101")
    cached_bytes = Path(active.pdf_cache_path).read_bytes()
    original_bytes = Path(FIXTURE_PATH).read_bytes()
    assert cached_bytes == original_bytes


def test_sync_retries_transient_fetch_failure_and_succeeds(conn, tmp_path, monkeypatch):
    """3 retries: a fetch that fails twice then succeeds must still apply normally
    and must not record a sync failure."""
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

    notifier = RecordingNotifier()
    result = asyncio.run(
        check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier)
    )

    assert result.status == "initial"
    assert call_count == 3
    assert storage.get_setting(conn, "last_sync_failed_at") in (None, "")


def test_sync_records_failure_after_exhausting_retries(conn, tmp_path, monkeypatch):
    import schedule_bot.sync as sync_module

    def always_fails(source):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", always_fails)

    notifier = RecordingNotifier()
    with pytest.raises(ConnectionError):
        asyncio.run(
            check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier)
        )

    failed_at = storage.get_setting(conn, "last_sync_failed_at")
    assert failed_at


def test_sync_clears_failure_flag_on_next_successful_sync(conn, tmp_path, monkeypatch):
    import schedule_bot.sync as sync_module

    def always_fails(source):
        raise ConnectionError("network unreachable")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", always_fails)

    notifier = RecordingNotifier()
    with pytest.raises(ConnectionError):
        asyncio.run(
            check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier)
        )
    assert storage.get_setting(conn, "last_sync_failed_at")

    monkeypatch.undo()

    result = asyncio.run(
        check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier)
    )

    assert result.status == "initial"
    assert storage.get_setting(conn, "last_sync_failed_at") in (None, "")



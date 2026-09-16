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


def test_first_sync_seeds_active_version_without_notifying(conn, tmp_path):
    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    notifier = RecordingNotifier()

    result = asyncio.run(
        check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier)
    )

    assert result.status == "initial"
    assert notifier.sent == []
    active = storage.get_active_version(conn, "Б-Мв-101")
    assert active is not None


def test_second_sync_with_same_pdf_reports_no_changes(conn, tmp_path):
    notifier = RecordingNotifier()
    asyncio.run(check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier))

    result = asyncio.run(
        check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier)
    )

    assert result.status == "no_changes"
    assert notifier.sent == []


def test_sync_with_changed_pdf_notifies_all_subscribers(conn, tmp_path, monkeypatch):
    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    storage.add_subscriber(conn, 222, "Б-Мв-101", "2026-09-16T00:00:00")
    notifier = RecordingNotifier()
    asyncio.run(check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier))

    # Simulate the PDF changing by monkeypatching parse_schedule's diff outcome:
    # force a fake non-empty diff via a stub parse_schedule that flips one lesson.
    import schedule_bot.sync as sync_module
    from schedule_bot.pdf_parser import parse_schedule as real_parse_schedule

    def fake_parse_schedule(source, group_name):
        schedule = real_parse_schedule(source, group_name)
        wednesday = schedule.days["Среда"]
        wednesday.lessons[0].upper = "ИЗМЕНЁННЫЙ ПРЕДМЕТ"
        return schedule

    monkeypatch.setattr(sync_module, "parse_schedule", fake_parse_schedule)

    result = asyncio.run(
        check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier)
    )

    assert result.status == "applied"
    assert len(result.changes) >= 1
    assert {chat_id for chat_id, _ in notifier.sent} == {111, 222}


def test_sync_caches_pdf_file_on_disk(conn, tmp_path):
    notifier = RecordingNotifier()
    asyncio.run(check_and_apply_diff(conn, FIXTURE_PATH, "Б-Мв-101", str(tmp_path), notifier))
    active = storage.get_active_version(conn, "Б-Мв-101")
    assert Path(active.pdf_cache_path).exists()

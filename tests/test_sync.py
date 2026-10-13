import asyncio
import sqlite3
from pathlib import Path

import pytest

from schedule_bot import storage
from schedule_bot.sync import sync_institute_link

FIXTURE_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:", check_same_thread=False)  # sync runs blocking work via asyncio.to_thread, like production (main.py)
    storage.init_db(connection)
    return connection


def _make_institute(conn):
    return storage.create_institute(
        conn, name="ИТ", catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )


def _make_link_and_groups(conn, institute_id, pdf_url, names):
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url=pdf_url, group_names=names, raw_label=",".join(names), last_seen_at="t"
    )
    group_ids = [storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name=name) for name in names]
    return link_id, group_ids


class RecordingNotifier:
    def __init__(self):
        self.sent = []

    async def __call__(self, chat_id, text):
        self.sent.append((chat_id, text))


def test_sync_institute_link_skips_network_when_no_active_groups(conn, tmp_path, monkeypatch):
    import schedule_bot.sync as sync_module

    institute_id = _make_institute(conn)
    link_id, _ = _make_link_and_groups(conn, institute_id, FIXTURE_PATH, ["Б-Мв-101"])

    def fail(*args, **kwargs):
        raise AssertionError("must not fetch when no active groups")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", fail)

    results = asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state={},
    ))
    assert results == []


def test_sync_institute_link_skips_network_when_stale(conn, tmp_path, monkeypatch):
    import schedule_bot.sync as sync_module

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(conn, institute_id, FIXTURE_PATH, ["Б-Мв-101"])
    storage.upsert_member(conn, 111, group_ids[0], joined_at="t")
    storage.mark_stale_links_not_in(conn, institute_id, confirmed_link_ids=[])

    def fail(*args, **kwargs):
        raise AssertionError("must not fetch a stale link")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", fail)

    results = asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state={},
    ))
    assert results == []


def test_sync_institute_link_fetches_and_parses_once_for_two_groups(conn, tmp_path, monkeypatch):
    import schedule_bot.pdf_parser as pdf_parser_module
    import schedule_bot.sync as sync_module

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(conn, institute_id, FIXTURE_PATH, ["Б-Мв-101", "Б-Эв-101"])
    for group_id in group_ids:
        storage.upsert_member(conn, group_id * 1000, group_id, joined_at="t")

    real_extract = pdf_parser_module.extract_raw_rows
    call_count = 0

    def counting_extract(source):
        nonlocal call_count
        call_count += 1
        return real_extract(source)

    monkeypatch.setattr(pdf_parser_module, "extract_raw_rows", counting_extract)

    results = asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state={},
    ))

    assert call_count == 1
    assert {r.status for r in results} == {"initial"}
    assert len(results) == 2


def test_sync_institute_link_fetches_pdf_bytes_once_for_two_groups(conn, tmp_path, monkeypatch):
    """Review-focus guard: both the byte fetch AND the pdfplumber parse
    must happen exactly once per link per sync call, regardless of how
    many groups are subscribed to that link. The brief's own test above
    only counts extract_raw_rows calls; this test additionally counts
    _load_pdf_bytes calls to pin down the network-fetch side too."""
    import schedule_bot.sync as sync_module

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(conn, institute_id, FIXTURE_PATH, ["Б-Мв-101", "Б-Эв-101"])
    for group_id in group_ids:
        storage.upsert_member(conn, group_id * 1000, group_id, joined_at="t")

    real_load = sync_module._load_pdf_bytes
    fetch_calls = []

    def counting_load(source):
        fetch_calls.append(source)
        return real_load(source)

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", counting_load)

    results = asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state={},
    ))

    assert len(fetch_calls) == 1
    assert fetch_calls[0] == FIXTURE_PATH
    assert len(results) == 2


def test_sync_institute_link_writes_separate_cache_copy_per_group(conn, tmp_path):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(conn, institute_id, FIXTURE_PATH, ["Б-Мв-101", "Б-Эв-101"])
    for group_id in group_ids:
        storage.upsert_member(conn, group_id * 1000, group_id, joined_at="t")

    asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state={},
    ))

    cache_paths = {Path(storage.get_active_version(conn, group_id).pdf_cache_path) for group_id in group_ids}
    assert len(cache_paths) == 2
    for path in cache_paths:
        assert path.exists()
        assert f"link_{link_id}" in str(path)


def test_sync_institute_link_sets_last_sync_failed_at_on_all_groups_on_fetch_failure(conn, tmp_path, monkeypatch):
    import schedule_bot.sync as sync_module

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(conn, institute_id, "https://example.com/dead.pdf", ["Б-Мв-101", "Б-Эв-101"])
    for group_id in group_ids:
        storage.upsert_member(conn, group_id * 1000, group_id, joined_at="t")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", lambda source: (_ for _ in ()).throw(ConnectionError("404")))
    monkeypatch.setattr(sync_module, "refresh_institute_catalog", lambda *a, **k: None)  # 404-fallback no-ops

    results = asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state={},
    ))

    assert all(r.status == "error" for r in results)
    for group_id in group_ids:
        assert storage.get_group(conn, group_id).last_sync_failed_at


def test_sync_institute_link_sets_last_sync_failed_at_only_on_group_with_missing_column(conn, tmp_path):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(conn, institute_id, FIXTURE_PATH, ["Б-Мв-101", "НЕТ-ТАКОЙ-ГРУППЫ"])
    for group_id in group_ids:
        storage.upsert_member(conn, group_id * 1000, group_id, joined_at="t")

    results = asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state={},
    ))

    good_group, bad_group = group_ids
    assert storage.get_group(conn, good_group).last_sync_failed_at in (None, "")
    assert storage.get_group(conn, bad_group).last_sync_failed_at
    statuses = {r.group_id: r.status for r in results}
    assert statuses[good_group] == "initial"
    assert statuses[bad_group] == "error"


def test_sync_institute_link_catalog_refresh_rate_limited_to_once_per_cycle(conn, tmp_path, monkeypatch):
    import schedule_bot.sync as sync_module

    institute_id = _make_institute(conn)
    link_a, group_ids_a = _make_link_and_groups(conn, institute_id, "https://example.com/dead-a.pdf", ["A"])
    link_b, group_ids_b = _make_link_and_groups(conn, institute_id, "https://example.com/dead-b.pdf", ["B"])
    storage.upsert_member(conn, 111, group_ids_a[0], joined_at="t")
    storage.upsert_member(conn, 222, group_ids_b[0], joined_at="t")

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", lambda source: (_ for _ in ()).throw(ConnectionError("404")))
    refresh_calls = []
    monkeypatch.setattr(sync_module, "refresh_institute_catalog", lambda conn, institute, **kwargs: refresh_calls.append(institute.id))

    shared_cycle_flag: dict[int, float] = {}
    asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_a), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state=shared_cycle_flag,
    ))
    asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_b), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state=shared_cycle_flag,
    ))

    assert refresh_calls == [institute_id]  # second 404 within the same check interval does not refresh again


def test_sync_institute_link_rereads_pdf_url_from_db_after_catalog_refresh(conn, tmp_path, monkeypatch):
    import schedule_bot.sync as sync_module

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(conn, institute_id, "https://example.com/dead.pdf", ["Б-Мв-101"])
    storage.upsert_member(conn, 111, group_ids[0], joined_at="t")

    def fake_load(source):
        if source == "https://example.com/dead.pdf":
            raise ConnectionError("404")
        return Path(FIXTURE_PATH).read_bytes()

    monkeypatch.setattr(sync_module, "_load_pdf_bytes", fake_load)

    def fake_refresh(conn, institute, **kwargs):
        storage.update_institute_link_pdf_url(conn, link_id, FIXTURE_PATH)

    monkeypatch.setattr(sync_module, "refresh_institute_catalog", fake_refresh)

    results = asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state={},
    ))

    assert results[0].status == "initial"


def test_sync_institute_link_notifies_subscribers_not_just_members(conn, tmp_path):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(conn, institute_id, FIXTURE_PATH, ["Б-Мв-101"])
    storage.upsert_member(conn, 111, group_ids[0], joined_at="t", subscribed=True)
    storage.upsert_member(conn, 222, group_ids[0], joined_at="t", subscribed=False)
    notifier = RecordingNotifier()
    asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), notifier, catalog_rescan_state={},
    ))
    # First sync is "initial" -> no notifications at all regardless of subscription.
    assert notifier.sent == []


def test_sync_institute_link_notifies_only_subscribed_member_on_change(conn, tmp_path):
    """Second sync pass that produces an actual change must notify only
    the subscribed member (subscribed_only=True), even though the gate
    for whether to sync at all considers all members (subscribed_only=False)."""
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(conn, institute_id, FIXTURE_PATH, ["Б-Мв-101"])
    group_id = group_ids[0]
    storage.upsert_member(conn, 111, group_id, joined_at="t", subscribed=True)
    storage.upsert_member(conn, 222, group_id, joined_at="t", subscribed=False)

    # First sync: establishes the "initial" active version.
    asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state={},
    ))

    # Force a change on the next sync by tampering with a lesson's text in
    # the stored active payload (compute_diff only compares per-day lesson
    # text, not the table bbox, so that's what must be mutated).
    active = storage.get_active_version(conn, group_id)
    import json
    payload = json.loads(active.payload_json)
    mutated = False
    for day in payload["days"].values():
        for lesson in day["lessons"]:
            lesson["upper"] = "ТАМПЕРЕД ДЛЯ ТЕСТА"
            mutated = True
    assert mutated, "fixture PDF must have at least one lesson to tamper with"
    conn.execute(
        "UPDATE schedule_versions SET payload_json = ? WHERE group_id = ? AND status = 'active'",
        (json.dumps(payload), group_id),
    )
    conn.commit()

    notifier = RecordingNotifier()
    results = asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), notifier, catalog_rescan_state={},
    ))

    assert results[0].status == "applied"
    assert [chat_id for chat_id, _ in notifier.sent] == [111]


def test_sync_forwards_catalog_rescan_alerts_to_admin(conn, tmp_path, monkeypatch):
    """Spec 1.3's "ссылка пропала из каталога" alert is most likely to be
    produced exactly here (PDF 404 -> catalog rescan -> link gone), and it
    can never fire again later (the newly-stale diff only looks at
    previously-live links). It must therefore reach the admin, not a
    `lambda text: None`."""
    import schedule_bot.sync as sync_module

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(conn, institute_id, "https://example.com/dead.pdf", ["Б-Мв-101"])
    storage.upsert_member(conn, 111, group_ids[0], joined_at="t")

    monkeypatch.setattr(
        sync_module, "_load_pdf_bytes", lambda source: (_ for _ in ()).throw(ConnectionError("404"))
    )

    def fake_refresh(conn_, institute, *, notify_admin):
        notify_admin("Ссылка института ИТ (id=1) пропала из каталога, подписчиков затронуто: 1")

    monkeypatch.setattr(sync_module, "refresh_institute_catalog", fake_refresh)

    admin_messages = []

    async def notify_admin(text):
        admin_messages.append(text)

    asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_rescan_state={}, notify_admin=notify_admin,
    ))

    assert any("пропала из каталога" in text for text in admin_messages)


def test_sync_allows_catalog_rescan_again_after_check_interval_elapsed(conn, tmp_path, monkeypatch):
    """The rate limit must be self-resetting: a stale per-process "already
    refreshed" flag would block the 404 fallback forever."""
    import schedule_bot.sync as sync_module

    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(conn, institute_id, "https://example.com/dead.pdf", ["Б-Мв-101"])
    storage.upsert_member(conn, 111, group_ids[0], joined_at="t")

    monkeypatch.setattr(
        sync_module, "_load_pdf_bytes", lambda source: (_ for _ in ()).throw(ConnectionError("404"))
    )
    refresh_calls = []
    monkeypatch.setattr(
        sync_module, "refresh_institute_catalog",
        lambda conn_, institute, **kwargs: refresh_calls.append(institute.id),
    )

    institute = storage.get_institute(conn, institute_id)  # check_interval_minutes == 60
    state: dict[int, float] = {}
    link = storage.get_institute_link(conn, link_id)

    asyncio.run(sync_institute_link(
        conn, link, institute, str(tmp_path), RecordingNotifier(), catalog_rescan_state=state,
    ))
    # Still inside the interval -> no second rescan.
    asyncio.run(sync_institute_link(
        conn, link, institute, str(tmp_path), RecordingNotifier(), catalog_rescan_state=state,
    ))
    # Pretend a full check interval has passed.
    state[institute_id] -= 61 * 60
    asyncio.run(sync_institute_link(
        conn, link, institute, str(tmp_path), RecordingNotifier(), catalog_rescan_state=state,
    ))

    assert refresh_calls == [institute_id, institute_id]

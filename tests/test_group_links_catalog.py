import json
import sqlite3

import pytest

from schedule_bot import storage
from schedule_bot.group_links import looks_like_group_name, refresh_institute_catalog


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _make_institute(conn):
    return storage.create_institute(
        conn, name="ИТ", catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )


def _two_level_fetch(pdf_html: str, form_label: str = "Очно-заочная форма обучения"):
    """Mock for fetch_source_page_html covering the two-level catalog
    shape: the first call (institute.catalog_url) returns a page with
    exactly one form-of-study link; every other call (the form page
    itself) returns pdf_html."""
    catalog_html = f'<a href="/forma">{form_label}</a>'

    def fetch(url):
        if url == "https://example.com/catalog":
            return catalog_html
        return pdf_html

    return fetch


def test_looks_like_group_name_accepts_letter_dash_digits():
    assert looks_like_group_name("Б-А-301") is True
    assert looks_like_group_name("Б-Мв-101") is True


def test_looks_like_group_name_accepts_letter_only_suffix():
    assert looks_like_group_name("Б-ТОП") is True
    assert looks_like_group_name("М-УК-О") is True
    assert looks_like_group_name("С-БиБ") is True


def test_looks_like_group_name_rejects_free_text():
    assert looks_like_group_name("Приказ №12 от 01.09") is False
    assert looks_like_group_name("1 курс БЭ, БМ оз") is False
    assert looks_like_group_name("Политика в отношении персональных данных") is False


def test_refresh_institute_catalog_creates_links_and_groups(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(
        group_links_module, "fetch_source_page_html",
        _two_level_fetch('<a href="/a.pdf">Б-А-301,302</a>'),
    )

    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert json.loads(links[0].group_names_json) == ["Б-А-301", "Б-А-302"]
    groups = storage.list_groups_for_link(conn, links[0].id)
    assert {g.name for g in groups} == {"Б-А-301", "Б-А-302"}
    assert storage.get_institute(conn, institute_id).last_catalog_sync_at is not None


def test_refresh_institute_catalog_dedups_same_pdf_url_two_labels_in_one_scan(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(
        group_links_module, "fetch_source_page_html",
        _two_level_fetch('<a href="/a.pdf">Б-А-301</a><a href="/a.pdf">Б-А-302</a>'),
    )

    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert set(json.loads(links[0].group_names_json)) == {"Б-А-301", "Б-А-302"}


def test_refresh_institute_catalog_same_pdf_url_second_scan_updates_names(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", _two_level_fetch('<a href="/a.pdf">Б-А-301</a>'))
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)
    first_link_id = storage.list_institute_links(conn, institute_id)[0].id

    monkeypatch.setattr(group_links_module, "fetch_source_page_html", _two_level_fetch('<a href="/a.pdf">Б-А-301,302</a>'))
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert links[0].id == first_link_id
    assert set(json.loads(links[0].group_names_json)) == {"Б-А-301", "Б-А-302"}


def test_refresh_institute_catalog_new_pdf_url_same_names_is_treated_as_moved_link(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", _two_level_fetch('<a href="/old.pdf">Б-А-301</a>'))
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)
    first_link_id = storage.list_institute_links(conn, institute_id)[0].id
    group_id = storage.list_groups_for_link(conn, first_link_id)[0].id

    monkeypatch.setattr(group_links_module, "fetch_source_page_html", _two_level_fetch('<a href="/new.pdf">Б-А-301</a>'))
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert links[0].id == first_link_id  # same row, pdf_url updated in place
    assert links[0].pdf_url == "https://example.com/new.pdf"
    assert storage.get_group(conn, group_id).institute_link_id == first_link_id  # group binding untouched


def test_refresh_institute_catalog_marks_unconfirmed_links_stale_and_alerts_once(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", _two_level_fetch('<a href="/a.pdf">Б-А-301</a>'))
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)
    link_id = storage.list_institute_links(conn, institute_id)[0].id
    group_id = storage.list_groups_for_link(conn, link_id)[0].id
    storage.upsert_member(conn, 111, group_id, joined_at="t")

    # An unrelated PDF link that IS present keeps raw_entries non-empty,
    # so this represents "this one link disappeared from an otherwise-
    # working catalog" (per-link stale-marking), not "the whole scan
    # found nothing" (covered separately below).
    alerts = []
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", _two_level_fetch('<a href="/unrelated.pdf">Z-Z-999</a>'))
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=alerts.append)

    assert storage.get_institute_link(conn, link_id).is_stale is True
    assert len(alerts) == 1
    assert str(institute_id) in alerts[0] or "пропала из каталога" in alerts[0]


def test_refresh_institute_catalog_zero_form_links_alerts_and_does_not_touch_db(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", _two_level_fetch('<a href="/a.pdf">Б-А-301</a>'))
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    alerts = []
    # Catalog page itself now has no "форма обучения" links at all.
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: "<html>no nav links here</html>")
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=alerts.append)

    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert links[0].is_stale is False  # nothing was touched
    assert len(alerts) == 1
    assert "форм обучения" in alerts[0]


def test_refresh_institute_catalog_zero_pdf_links_across_all_forms_alerts_and_does_not_touch_db(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", _two_level_fetch('<a href="/a.pdf">Б-А-301</a>'))
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    alerts = []
    # Form-of-study link present, but its page has zero PDF links.
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", _two_level_fetch("<html>empty, no pdfs</html>"))
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=alerts.append)

    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert links[0].is_stale is False
    assert any("0 ссылок" in alert for alert in alerts)


def test_refresh_institute_catalog_zero_pdf_links_mentions_unreachable_form_count_when_all_down(conn, monkeypatch):
    """Regression test: when 0 PDF links is caused by EVERY form-of-study
    page being unreachable (not by a reachable-but-empty page), the alert
    must say so — a bare "0 ссылок" reads as "institute genuinely has no
    PDFs", hiding that the real cause is a site outage."""
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    catalog_html = (
        '<a href="/ochnaya">Очная форма обучения</a>'
        '<a href="/zaochnaya">Заочная форма обучения</a>'
    )

    def fetch_all_down(url):
        if url == "https://example.com/catalog":
            return catalog_html
        raise group_links_module.SourcePageUnreachableError("boom")

    monkeypatch.setattr(group_links_module, "fetch_source_page_html", fetch_all_down)
    alerts = []
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=alerts.append)

    assert len(alerts) == 1
    assert "2" in alerts[0]
    assert "недоступ" in alerts[0]


def test_refresh_institute_catalog_filters_out_non_group_looking_labels(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", _two_level_fetch('<a href="/a.pdf">1 курс БЭ, БМ оз</a>'))

    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 1
    assert json.loads(links[0].group_names_json) == []
    assert storage.list_groups_for_link(conn, links[0].id) == []


def test_refresh_institute_catalog_conflict_keeps_existing_binding(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(
        group_links_module, "fetch_source_page_html",
        _two_level_fetch('<a href="/a.pdf">Б-А-301</a><a href="/b.pdf">Б-А-301</a>'),
    )
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    link_a = storage.get_institute_link_by_pdf_url(conn, institute_id, "https://example.com/a.pdf")
    group = storage.get_group_by_name(conn, institute_id, "Б-А-301")
    assert group.institute_link_id == link_a.id  # first link by page order wins, and is stable on rescans

    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)
    group_after = storage.get_group_by_name(conn, institute_id, "Б-А-301")
    assert group_after.institute_link_id == link_a.id  # unchanged — binding isn't re-decided every scan


def test_refresh_institute_catalog_two_noise_links_with_no_valid_names_stay_distinct(conn, monkeypatch):
    """Regression test: phase-2 identity matching compares group-name
    SETS, and set() == set() is vacuously True for any two links that
    both filtered down to zero valid group names. Without a guard, a
    brand-new noise link at a never-before-seen pdf_url would spuriously
    match ANY pre-existing noise link via the empty-set comparison."""
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(
        group_links_module, "fetch_source_page_html",
        _two_level_fetch('<a href="/orders.pdf">Приказ №12 от 01.09</a>'),
    )
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)
    first_links = storage.list_institute_links(conn, institute_id)
    assert len(first_links) == 1
    noise_link_id = first_links[0].id

    monkeypatch.setattr(
        group_links_module, "fetch_source_page_html",
        _two_level_fetch('<a href="/rules.pdf">Методические указания</a>'),
    )
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    links = storage.list_institute_links(conn, institute_id)
    assert len(links) == 2
    links_by_id = {link.id: link for link in links}

    old_link = links_by_id[noise_link_id]
    assert old_link.pdf_url == "https://example.com/orders.pdf"
    assert old_link.is_stale is True

    new_link = next(link for link in links if link.id != noise_link_id)
    assert new_link.pdf_url == "https://example.com/rules.pdf"
    assert new_link.is_stale is False


def test_refresh_institute_catalog_does_not_mutate_db_on_zero_form_links(conn, monkeypatch):
    """Explicit safety test: a catalog scan that finds zero form-of-study
    links must not call any storage mutation function at all — not
    mark_stale_links_not_in, not set_institute_last_catalog_sync_at."""
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", _two_level_fetch('<a href="/a.pdf">Б-А-301</a>'))
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    before = storage.get_institute(conn, institute_id)
    before_links = storage.list_institute_links(conn, institute_id)

    called = {"mark_stale": False, "sync_at": False}
    original_mark_stale = storage.mark_stale_links_not_in
    original_sync_at = storage.set_institute_last_catalog_sync_at

    def _tracked_mark_stale(*args, **kwargs):
        called["mark_stale"] = True
        return original_mark_stale(*args, **kwargs)

    def _tracked_sync_at(*args, **kwargs):
        called["sync_at"] = True
        return original_sync_at(*args, **kwargs)

    monkeypatch.setattr(group_links_module.storage, "mark_stale_links_not_in", _tracked_mark_stale)
    monkeypatch.setattr(group_links_module.storage, "set_institute_last_catalog_sync_at", _tracked_sync_at)

    alerts = []
    monkeypatch.setattr(group_links_module, "fetch_source_page_html", lambda url: "<html>no links here</html>")
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=alerts.append)

    assert called["mark_stale"] is False
    assert called["sync_at"] is False
    assert len(alerts) == 1
    after = storage.get_institute(conn, institute_id)
    after_links = storage.list_institute_links(conn, institute_id)
    assert after.last_catalog_sync_at == before.last_catalog_sync_at
    assert len(after_links) == len(before_links)
    assert after_links[0].is_stale == before_links[0].is_stale


def test_refresh_institute_catalog_aggregates_links_across_multiple_form_pages(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    catalog_html = (
        '<a href="/ochnaya">Очная форма обучения</a>'
        '<a href="/zaochnaya">Заочная форма обучения</a>'
    )

    def fetch(url):
        if url == "https://example.com/catalog":
            return catalog_html
        if url == "https://example.com/ochnaya":
            return '<a href="/a.pdf">Б-А-301</a>'
        if url == "https://example.com/zaochnaya":
            return '<a href="/b.pdf">Б-Зв-201</a>'
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(group_links_module, "fetch_source_page_html", fetch)

    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    groups = storage.list_all_groups(conn)
    assert {g.name for g in groups} == {"Б-А-301", "Б-Зв-201"}
    assert len(storage.list_institute_links(conn, institute_id)) == 2


def test_refresh_institute_catalog_does_not_mark_stale_when_a_form_page_is_unreachable(conn, monkeypatch):
    """Regression test for review finding: a transient fetch failure of
    ONE form-of-study page must not be treated as "that form's links are
    gone from the catalog" — it must not mark any pre-existing link
    stale, since that stops its sync entirely until the next (weekly)
    successful full scan."""
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    catalog_html = (
        '<a href="/ochnaya">Очная форма обучения</a>'
        '<a href="/zaochnaya">Заочная форма обучения</a>'
    )

    def fetch_both_ok(url):
        if url == "https://example.com/catalog":
            return catalog_html
        if url == "https://example.com/ochnaya":
            return '<a href="/a.pdf">Б-А-301</a>'
        if url == "https://example.com/zaochnaya":
            return '<a href="/b.pdf">Б-Зв-201</a>'
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(group_links_module, "fetch_source_page_html", fetch_both_ok)
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)
    link_a = storage.get_institute_link_by_pdf_url(conn, institute_id, "https://example.com/a.pdf")
    group_a = storage.list_groups_for_link(conn, link_a.id)[0]
    storage.upsert_member(conn, 333, group_a.id, joined_at="t")

    def fetch_ochnaya_down(url):
        if url == "https://example.com/catalog":
            return catalog_html
        if url == "https://example.com/ochnaya":
            raise group_links_module.SourcePageUnreachableError("boom")
        if url == "https://example.com/zaochnaya":
            return '<a href="/b.pdf">Б-Зв-201</a>'
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(group_links_module, "fetch_source_page_html", fetch_ochnaya_down)
    alerts = []
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=alerts.append)

    assert storage.get_institute_link(conn, link_a.id).is_stale is False
    assert alerts == []


def test_refresh_institute_catalog_skips_unreachable_form_page_but_keeps_others(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    catalog_html = (
        '<a href="/ochnaya">Очная форма обучения</a>'
        '<a href="/zaochnaya">Заочная форма обучения</a>'
    )

    def fetch(url):
        if url == "https://example.com/catalog":
            return catalog_html
        if url == "https://example.com/ochnaya":
            raise group_links_module.SourcePageUnreachableError("boom")
        if url == "https://example.com/zaochnaya":
            return '<a href="/b.pdf">Б-Зв-201</a>'
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr(group_links_module, "fetch_source_page_html", fetch)

    alerts = []
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=alerts.append)

    groups = storage.list_all_groups(conn)
    assert {g.name for g in groups} == {"Б-Зв-201"}
    assert alerts == []  # an unreachable form page this cycle is tolerated silently (logged, not alerted)


def test_catalog_lock_for_returns_same_lock_for_same_institute():
    import asyncio

    from schedule_bot.sync import catalog_lock_for

    locks: dict[int, asyncio.Lock] = {}
    lock_a = catalog_lock_for(locks, 7)
    lock_b = catalog_lock_for(locks, 7)
    assert lock_a is lock_b


@pytest.mark.asyncio
async def test_two_concurrent_catalog_scans_of_same_institute_serialize():
    import asyncio

    from schedule_bot.sync import catalog_lock_for

    locks: dict[int, asyncio.Lock] = {}
    events: list[str] = []

    async def scan(tag: str):
        lock = catalog_lock_for(locks, 1)
        async with lock:
            events.append(f"{tag}:start")
            await asyncio.sleep(0.01)
            events.append(f"{tag}:end")

    await asyncio.gather(scan("A"), scan("B"))
    assert events in (["A:start", "A:end", "B:start", "B:end"], ["B:start", "B:end", "A:start", "A:end"])


def test_second_pdf_with_same_group_names_does_not_steal_the_first_links_row(conn, monkeypatch):
    import schedule_bot.group_links as group_links_module

    institute_id = _make_institute(conn)
    monkeypatch.setattr(
        group_links_module, "fetch_source_page_html",
        _two_level_fetch('<a href="/a.pdf">Б-А-301</a>'),
    )
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    links_after_scan_1 = storage.list_institute_links(conn, institute_id)
    assert {l.pdf_url for l in links_after_scan_1} == {"https://example.com/a.pdf"}

    monkeypatch.setattr(
        group_links_module, "fetch_source_page_html",
        _two_level_fetch('<a href="/a.pdf">Б-А-301</a><a href="/a_s_12_10_2026.pdf">Б-А-301</a>'),
    )
    refresh_institute_catalog(conn, storage.get_institute(conn, institute_id), notify_admin=lambda text: None)

    links_after_scan_2 = storage.list_institute_links(conn, institute_id)
    urls_after_scan_2 = {l.pdf_url for l in links_after_scan_2 if not l.is_stale}
    assert "https://example.com/a.pdf" in urls_after_scan_2
    assert "https://example.com/a_s_12_10_2026.pdf" in urls_after_scan_2

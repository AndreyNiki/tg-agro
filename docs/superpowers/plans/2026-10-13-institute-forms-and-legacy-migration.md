# Formы Обучения в Каталоге Института и Миграция Легаси-Пользователей Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make institute catalog scanning follow the real two-level page
structure (catalog page → up to three "форма обучения" subpages → PDF
links), and add a one-time, env-var-gated migration that moves every
existing `members` row onto one target group.

**Architecture:** `group_links.refresh_institute_catalog` gains a
preliminary fetch+filter step that discovers form-of-study subpage links
on the institute's catalog page, then fetches each one and feeds their
combined PDF links into the existing (unchanged) two-phase upsert
pipeline. A new `schedule_bot/legacy_migration.py` module reuses that
same `refresh_institute_catalog` plus the existing
`validate_and_sync_group_on_first_subscribe` subscribe-time validation
path, gated by three required env vars and a one-time `settings` flag,
wired in from `main.py`'s `_post_init`.

**Tech Stack:** Python 3, sqlite3, python-telegram-bot, pytest +
pytest-asyncio, `unittest.mock`.

**Spec:** `docs/superpowers/specs/2026-10-13-institute-forms-and-legacy-migration-design.md`

## Global Constraints

- No backward compatibility with the old "catalog page = direct PDF
  list" shape — this feature has not shipped yet, so
  `refresh_institute_catalog` unconditionally expects form-of-study
  subpages; a catalog page with zero such links is treated as a failed
  scan (log + one admin alert + no DB writes), not a fallback.
- `refresh_institute_catalog`'s public signature
  (`conn, institute, *, notify_admin`) does not change — callers in
  `institute_commands.py` and the new `legacy_migration.py` are
  unaffected by its internals.
- All three `LEGACY_MIGRATION_*` env vars are required together; if even
  one is missing/empty, the migration is fully disabled (no logs, no
  DB reads for it).
- The migration runs at most once ever, tracked by a `settings` row
  (`legacy_migration_done_at`) — never re-run once that key is set, even
  if the env vars remain configured on a later restart. This protects
  any manual `/change_group` a migrated user performs afterward.
- Any step of the migration that fails (catalog unreachable, target
  group not found, PDF validation fails) must leave the `settings` flag
  unset and alert the admin, so the next restart retries from scratch —
  never leave it half-done with the flag set.
- Blocking I/O (`fetch_source_page_html`, PDF download+parse inside
  `validate_and_sync_group_on_first_subscribe`) must run through
  `asyncio.to_thread` from any `async` call site, per the existing
  project-wide rule (`concurrent_updates(True)`).

## Review Focus

- An institute catalog page that lists only one or two of the three
  known forms of study (not all three) — the scan must still succeed
  using whichever subset is present, not require all three.
- A form-of-study subpage whose fetch fails (site hiccup, 404) must not
  abort the whole catalog scan for the institute's *other* reachable
  forms — only that one form's links are missing this cycle.
- `LEGACY_MIGRATION_GROUP_NAME` naming a group that exists in the
  institute's catalog but fails PDF column validation (typo, PDF
  reformatted) — migration must not partially apply (no members
  touched, flag not set) and must alert so it's visible, not silently
  stuck.
- A member who already has a `members` row with a *different* `group_id`
  before migration runs — migration must still move them (spec: "для
  всех существующих юзеров", not just ungrouped ones), not skip because
  they already belong to some other group.
- Running `run_legacy_migration_if_configured` twice in a row (e.g. two
  bot restarts before anyone unsets the env vars) must be a pure no-op
  the second time — no second catalog scan, no second admin alert, no
  double-reassignment.

---

### Task 1: `list_form_of_study_links`

**Files:**
- Modify: `schedule_bot/group_links.py`
- Test: `tests/test_group_links.py`

**Interfaces:**
- Produces: `list_form_of_study_links(html: str, base_url: str) -> list[tuple[str, str]]` — returns `[(absolute_url, normalized_label), ...]` for every `<a>` tag whose visible text contains "форма обучения" (case-insensitive), in document order. Consumed by Task 2.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_group_links.py`:

```python
from schedule_bot.group_links import list_form_of_study_links

_FORM_NAV_HTML = """
<html><body>
<a href="novosti">Новости</a>
<a href="ochnaya-forma-obucheniya">Очная форма обучения</a>
<a href="ochno-zaochnaya-forma-obucheniya">Очно-заочная форма обучения</a>
<a href="zaochnaya-forma-obucheniya">Заочная форма обучения</a>
</body></html>
"""


def test_list_form_of_study_links_finds_all_three_and_ignores_other_links():
    links = list_form_of_study_links(_FORM_NAV_HTML, "https://example.com/institut")
    assert links == [
        ("https://example.com/ochnaya-forma-obucheniya", "Очная форма обучения"),
        ("https://example.com/ochno-zaochnaya-forma-obucheniya", "Очно-заочная форма обучения"),
        ("https://example.com/zaochnaya-forma-obucheniya", "Заочная форма обучения"),
    ]


def test_list_form_of_study_links_returns_empty_when_none_present():
    html = '<a href="novosti">Новости</a>'
    assert list_form_of_study_links(html, "https://example.com/institut") == []


def test_list_form_of_study_links_is_case_insensitive_on_label():
    html = '<a href="x">ОЧНАЯ ФОРМА ОБУЧЕНИЯ</a>'
    assert list_form_of_study_links(html, "https://example.com/institut") == [
        ("https://example.com/x", "ОЧНАЯ ФОРМА ОБУЧЕНИЯ")
    ]


def test_list_form_of_study_links_resolves_relative_href_against_base_url():
    html = '<a href="/abs/path">Заочная форма обучения</a>'
    links = list_form_of_study_links(html, "https://example.com/deep/institut")
    assert links == [("https://example.com/abs/path", "Заочная форма обучения")]


def test_list_form_of_study_links_handles_subset_of_the_three_forms():
    html = '<a href="x">Очная форма обучения</a>'
    assert list_form_of_study_links(html, "https://example.com/institut") == [
        ("https://example.com/x", "Очная форма обучения")
    ]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_group_links.py -k form_of_study -v`
Expected: FAIL with `ImportError: cannot import name 'list_form_of_study_links'`

- [ ] **Step 3: Implement**

In `schedule_bot/group_links.py`, add a generic anchor regex (the
existing `_PDF_LINK_RE` only matches `href` ending in `.pdf`, which a
form-of-study link's `href` never does) and the new function. Insert
right after the existing `_PDF_LINK_RE`/`_TAG_RE` constants (around
line 13-19):

```python
_GENERIC_LINK_RE = re.compile(
    r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_FORM_OF_STUDY_LABEL_RE = re.compile(r"форма обучения", re.IGNORECASE)
```

Add the function right after `list_pdf_links` (after its closing
`return results`, currently ending at line 75):

```python
def list_form_of_study_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Returns [(url, label), ...] for every <a> on html whose visible
    text contains "форма обучения" (case-insensitive). The institute's
    catalog page links to up to three such subpages (Очная/Очно-заочная/
    Заочная form of study) and ONLY those subpages hold the actual PDF
    links — not every institute has all three, a form missing from the
    page's menu simply produces no entry here."""
    results = []
    for href, inner_html in _GENERIC_LINK_RE.findall(html):
        visible_text = _TAG_RE.sub("", inner_html)
        label = _normalize_label(visible_text)
        if _FORM_OF_STUDY_LABEL_RE.search(label):
            results.append((urljoin(base_url, href), label))
    return results
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_group_links.py -v`
Expected: all PASS, including the pre-existing tests in that file.

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/group_links.py tests/test_group_links.py
git commit -m "feat: add list_form_of_study_links for institute catalog navigation pages"
```

---

### Task 2: Two-level catalog scan in `refresh_institute_catalog`

**Files:**
- Modify: `schedule_bot/group_links.py`
- Test: `tests/test_group_links_catalog.py` (full rewrite — every existing test's mock changes shape, per Global Constraints: no backward-compat branch)

**Interfaces:**
- Consumes: `list_form_of_study_links` from Task 1; existing `fetch_source_page_html`, `list_pdf_links`, `SourcePageUnreachableError`.
- Produces: `refresh_institute_catalog(conn, institute, *, notify_admin)` — signature unchanged, behavior changed per spec section 1.2. Consumed as-is by `institute_commands.py` (unchanged) and by Task 5's `legacy_migration.py`.

- [ ] **Step 1: Write the failing tests (full file rewrite)**

Replace the entire contents of `tests/test_group_links_catalog.py` with:

```python
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


def test_looks_like_group_name_rejects_free_text():
    assert looks_like_group_name("Приказ №12 от 01.09") is False
    assert looks_like_group_name("1 курс БЭ, БМ оз") is False


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_group_links_catalog.py -v`
Expected: multiple FAILs — `refresh_institute_catalog` still fetches
`institute.catalog_url` directly for PDF links, so every test using
`_two_level_fetch` gets zero PDF links (the mock's first branch returns
only a form-of-study nav link, no `.pdf` anchors) and the new
multi-form/unreachable-form tests fail outright (single-fetch
implementation never calls a second URL).

- [ ] **Step 3: Implement**

In `schedule_bot/group_links.py`, replace the body of
`refresh_institute_catalog` from the start through the `labels_by_url`
construction (currently):

```python
    html = fetch_source_page_html(institute.catalog_url)
    raw_entries = list_pdf_links(html, institute.catalog_url)

    if not raw_entries:
        logger.warning("group_links: catalog for institute %r returned 0 links", institute.name)
        notify_admin(f"Каталог института {institute.name} (id={institute.id}) вернул 0 ссылок")
        return

    # Dedup same pdf_url appearing under two different <a> tags in one scan.
    labels_by_url: dict[str, list[str]] = {}
```

with:

```python
    html = fetch_source_page_html(institute.catalog_url)
    form_links = list_form_of_study_links(html, institute.catalog_url)

    if not form_links:
        logger.warning("group_links: catalog for institute %r returned 0 form-of-study links", institute.name)
        notify_admin(f"Каталог института {institute.name} (id={institute.id}) вернул 0 ссылок на формы обучения")
        return

    raw_entries: list[tuple[str, str]] = []
    for form_url, form_label in form_links:
        try:
            form_html = fetch_source_page_html(form_url)
        except SourcePageUnreachableError as error:
            logger.warning("group_links: form-of-study page %r (%s) unreachable: %s", form_label, form_url, error)
            continue
        raw_entries.extend(list_pdf_links(form_html, form_url))

    if not raw_entries:
        logger.warning("group_links: catalog for institute %r returned 0 PDF links across all form-of-study pages", institute.name)
        notify_admin(f"Каталог института {institute.name} (id={institute.id}) вернул 0 ссылок")
        return

    # Dedup same pdf_url appearing under two different <a> tags in one scan.
    labels_by_url: dict[str, list[str]] = {}
```

Everything from that point on (the `for pdf_url, raw_label in
raw_entries.items()` loop... — actually it iterates
`labels_by_url.items()`, unchanged — through the end of the function)
stays exactly as it is today; it already operates on the flat
`raw_entries`/`labels_by_url` structures regardless of how many pages
they were collected from.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_group_links_catalog.py tests/test_group_links.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite to check for collateral breakage**

Run: `./.venv/bin/python -m pytest -v`
Expected: all PASS. (`institute_commands.py`'s own tests mock
`refresh_institute_catalog` itself or `fetch_source_page_html` the same
way — if any other test file monkeypatches `fetch_source_page_html` for
`refresh_institute_catalog` with a single-page HTML string, it will now
fail the same way Task 2's old tests did; fix any such test the same
way, via a two-level mock.)

- [ ] **Step 6: Commit**

```bash
git add schedule_bot/group_links.py tests/test_group_links_catalog.py
git commit -m "feat: scan form-of-study subpages before looking for PDF links in institute catalog"
```

---

### Task 3: Storage helpers — `get_institute_by_catalog_url`, `reassign_member_group`

**Files:**
- Modify: `schedule_bot/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Produces: `storage.get_institute_by_catalog_url(conn, catalog_url: str) -> InstituteRow | None`; `storage.reassign_member_group(conn, chat_id: int, group_id: int) -> None`. Both consumed by Task 5.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_storage.py`:

```python
def test_get_institute_by_catalog_url_finds_existing(conn):
    institute_id = storage.create_institute(
        conn, name="Инст", catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )
    found = storage.get_institute_by_catalog_url(conn, "https://example.com/catalog")
    assert found is not None
    assert found.id == institute_id


def test_get_institute_by_catalog_url_returns_none_when_absent(conn):
    assert storage.get_institute_by_catalog_url(conn, "https://example.com/nope") is None


def test_reassign_member_group_updates_group_id_only(conn):
    institute_id = storage.create_institute(
        conn, name="Инст", catalog_url="https://example.com/catalog",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )
    link_id = storage.create_institute_link(
        conn, institute_id=institute_id, pdf_url="https://example.com/a.pdf",
        group_names=["А", "Б"], raw_label="А,Б", last_seen_at="t",
    )
    old_group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="А")
    new_group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name="Б")
    storage.upsert_member(conn, 555, old_group_id, joined_at="2026-01-01T00:00:00+00:00", subscribed=False)
    storage.update_member_profile(conn, 555, telegram_username="bob", display_name="Bob")

    storage.reassign_member_group(conn, 555, new_group_id)

    member = storage.get_member(conn, 555)
    assert member.group_id == new_group_id
    assert member.subscribed is False  # untouched
    assert member.joined_at == "2026-01-01T00:00:00+00:00"  # untouched
    assert member.telegram_username == "bob"  # untouched
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -k "catalog_url or reassign_member" -v`
Expected: FAIL with `AttributeError: module 'schedule_bot.storage' has no attribute 'get_institute_by_catalog_url'`

- [ ] **Step 3: Implement**

In `schedule_bot/storage.py`, add right after `get_institute` (currently
ending at line 315, just before `list_institutes`):

```python
def get_institute_by_catalog_url(conn: sqlite3.Connection, catalog_url: str) -> InstituteRow | None:
    row = conn.execute(
        f"SELECT {_INSTITUTE_COLUMNS} FROM institutes WHERE catalog_url = ?", (catalog_url,)
    ).fetchone()
    return InstituteRow(*row) if row else None
```

Add right after `update_member_profile` (currently ending at line 586,
just before `list_group_members`):

```python
def reassign_member_group(conn: sqlite3.Connection, chat_id: int, group_id: int) -> None:
    conn.execute("UPDATE members SET group_id = ? WHERE chat_id = ?", (group_id, chat_id))
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_storage.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "feat: add get_institute_by_catalog_url and reassign_member_group storage helpers"
```

---

### Task 4: Config support for `LEGACY_MIGRATION_*`

**Files:**
- Modify: `schedule_bot/config.py`
- Modify: `.env.example`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `Config.legacy_migration_catalog_url: str | None`, `Config.legacy_migration_institute_name: str | None`, `Config.legacy_migration_group_name: str | None`, and `Config.legacy_migration` (property) returning `tuple[str, str, str] | None` — `None` unless all three are non-empty. Consumed by Task 5.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_load_config_legacy_migration_is_none_when_vars_absent():
    config = load_config(_base_env())
    assert config.legacy_migration_catalog_url is None
    assert config.legacy_migration_institute_name is None
    assert config.legacy_migration_group_name is None
    assert config.legacy_migration is None


def test_load_config_legacy_migration_is_none_when_only_some_vars_present():
    env = _base_env()
    env["LEGACY_MIGRATION_INSTITUT"] = "https://example.com/institut"
    env["LEGACY_MIGRATION_INSTITUT_NAME"] = "Институт агробизнеса"
    # LEGACY_MIGRATION_GROUP_NAME intentionally missing
    config = load_config(env)
    assert config.legacy_migration is None


def test_load_config_legacy_migration_set_when_all_three_vars_present():
    env = _base_env()
    env["LEGACY_MIGRATION_INSTITUT"] = "https://example.com/institut"
    env["LEGACY_MIGRATION_INSTITUT_NAME"] = "Институт агробизнеса"
    env["LEGACY_MIGRATION_GROUP_NAME"] = "Б-Мв-101"
    config = load_config(env)
    assert config.legacy_migration == (
        "https://example.com/institut", "Институт агробизнеса", "Б-Мв-101",
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_config.py -k legacy_migration -v`
Expected: FAIL with `AttributeError: 'Config' object has no attribute 'legacy_migration_catalog_url'`

- [ ] **Step 3: Implement**

In `schedule_bot/config.py`, add three fields to the `Config` dataclass
(after `default_catalog_refresh_interval_minutes`) and a property:

```python
@dataclass
class Config:
    telegram_token: str
    admin_chat_id: int
    db_path: str
    pdf_cache_dir: str
    default_weektype_url: str
    default_daily_send_time: str
    default_check_interval_minutes: int
    default_timezone: str
    default_catalog_refresh_interval_minutes: int
    legacy_migration_catalog_url: str | None = None
    legacy_migration_institute_name: str | None = None
    legacy_migration_group_name: str | None = None

    @property
    def legacy_migration(self) -> tuple[str, str, str] | None:
        if self.legacy_migration_catalog_url and self.legacy_migration_institute_name and self.legacy_migration_group_name:
            return (
                self.legacy_migration_catalog_url,
                self.legacy_migration_institute_name,
                self.legacy_migration_group_name,
            )
        return None
```

In `load_config`, add to the returned `Config(...)` call (after
`default_catalog_refresh_interval_minutes=...`):

```python
        legacy_migration_catalog_url=env.get("LEGACY_MIGRATION_INSTITUT") or None,
        legacy_migration_institute_name=env.get("LEGACY_MIGRATION_INSTITUT_NAME") or None,
        legacy_migration_group_name=env.get("LEGACY_MIGRATION_GROUP_NAME") or None,
```

In `.env.example`, append:

```
# One-time migration of every existing `members` row onto one group —
# for moving users off the single hardcoded group the bot used before
# institutes existed. All three vars are required together; if even one
# is missing/empty, the migration is fully disabled. Once it succeeds,
# it is recorded in the `settings` table (legacy_migration_done_at) and
# never runs again, even if these stay set.
# LEGACY_MIGRATION_INSTITUT=https://www.vavilovsar.ru/.../institut-agrobiznesa
# LEGACY_MIGRATION_INSTITUT_NAME=Институт агробизнеса
# LEGACY_MIGRATION_GROUP_NAME=Б-Мв-101
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_config.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/config.py .env.example tests/test_config.py
git commit -m "feat: add optional LEGACY_MIGRATION_* config for one-time user migration"
```

---

### Task 5: `legacy_migration.py` module

**Files:**
- Create: `schedule_bot/legacy_migration.py`
- Test: `tests/test_legacy_migration.py`

**Interfaces:**
- Consumes: `storage.get_institute_by_catalog_url`, `storage.reassign_member_group`, `storage.get_setting`/`set_setting`, `storage.create_institute`, `storage.get_institute`, `storage.get_group_by_name`, `storage.list_all_member_chat_ids` (Task 3 + existing); `group_links.refresh_institute_catalog` (Task 2); `group_commands.validate_and_sync_group_on_first_subscribe` (existing, signature `async def (conn, group, pdf_cache_dir, notify_admin, sync_institute_link_fn) -> bool`); `Config.legacy_migration` (Task 4).
- Produces: `async def run_legacy_migration_if_configured(conn, config, notify_admin, sync_institute_link_fn, reschedule_institute_jobs) -> None` where `notify_admin` is `async def notify_admin(text: str) -> None` and `reschedule_institute_jobs` is a plain (non-async) `def reschedule_institute_jobs(institute_id: int) -> None`. Consumed by Task 6.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_legacy_migration.py`:

```python
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.config import Config
from schedule_bot.legacy_migration import run_legacy_migration_if_configured


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def _config(**overrides):
    base = dict(
        telegram_token="t", admin_chat_id=1, db_path=":memory:", pdf_cache_dir="/tmp/cache",
        default_weektype_url="https://example.com/week", default_daily_send_time="09:00",
        default_check_interval_minutes=60, default_timezone="Europe/Moscow",
        default_catalog_refresh_interval_minutes=10080,
        legacy_migration_catalog_url="https://example.com/institut",
        legacy_migration_institute_name="Институт агробизнеса",
        legacy_migration_group_name="Б-Мв-101",
    )
    base.update(overrides)
    return Config(**base)


@pytest.mark.asyncio
async def test_noop_when_not_configured(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    monkeypatch.setattr(mod, "refresh_institute_catalog", AsyncMock())
    config = _config(legacy_migration_group_name=None)
    notify_admin = AsyncMock()
    reschedule = MagicMock()

    await run_legacy_migration_if_configured(conn, config, notify_admin, AsyncMock(), reschedule)

    notify_admin.assert_not_awaited()
    reschedule.assert_not_called()
    assert storage.get_setting(conn, "legacy_migration_done_at") is None


@pytest.mark.asyncio
async def test_noop_when_already_done(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    storage.set_setting(conn, "legacy_migration_done_at", "2026-01-01T00:00:00+00:00")
    monkeypatch.setattr(mod, "refresh_institute_catalog", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not be called")))
    config = _config()
    notify_admin = AsyncMock()
    reschedule = MagicMock()

    await run_legacy_migration_if_configured(conn, config, notify_admin, AsyncMock(), reschedule)

    notify_admin.assert_not_awaited()
    reschedule.assert_not_called()


@pytest.mark.asyncio
async def test_creates_institute_when_not_found_by_catalog_url(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    def fake_refresh(conn_, institute, *, notify_admin):
        link_id = storage.create_institute_link(
            conn_, institute_id=institute.id, pdf_url="https://example.com/a.pdf",
            group_names=["Б-Мв-101"], raw_label="Б-Мв-101", last_seen_at="t",
        )
        storage.create_group(conn_, institute_id=institute.id, institute_link_id=link_id, name="Б-Мв-101")

    monkeypatch.setattr(mod, "refresh_institute_catalog", fake_refresh)
    monkeypatch.setattr(mod, "validate_and_sync_group_on_first_subscribe", AsyncMock(return_value=True))
    config = _config()
    notify_admin = AsyncMock()
    reschedule = MagicMock()

    await run_legacy_migration_if_configured(conn, config, notify_admin, AsyncMock(), reschedule)

    institute = storage.get_institute_by_catalog_url(conn, "https://example.com/institut")
    assert institute is not None
    assert institute.name == "Институт агробизнеса"
    reschedule.assert_called_once_with(institute.id)


@pytest.mark.asyncio
async def test_reuses_existing_institute_found_by_catalog_url(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    existing_id = storage.create_institute(
        conn, name="Уже есть", catalog_url="https://example.com/institut",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )

    def fake_refresh(conn_, institute, *, notify_admin):
        assert institute.id == existing_id
        link_id = storage.create_institute_link(
            conn_, institute_id=institute.id, pdf_url="https://example.com/a.pdf",
            group_names=["Б-Мв-101"], raw_label="Б-Мв-101", last_seen_at="t",
        )
        storage.create_group(conn_, institute_id=institute.id, institute_link_id=link_id, name="Б-Мв-101")

    monkeypatch.setattr(mod, "refresh_institute_catalog", fake_refresh)
    monkeypatch.setattr(mod, "validate_and_sync_group_on_first_subscribe", AsyncMock(return_value=True))
    config = _config()

    await run_legacy_migration_if_configured(conn, config, AsyncMock(), AsyncMock(), MagicMock())

    assert len(storage.list_institutes(conn)) == 1  # no duplicate created


@pytest.mark.asyncio
async def test_postpones_when_group_not_found_after_scan(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    def fake_refresh(conn_, institute, *, notify_admin):
        pass  # catalog scan finds nothing matching the configured group name

    monkeypatch.setattr(mod, "refresh_institute_catalog", fake_refresh)
    validate_mock = AsyncMock()
    monkeypatch.setattr(mod, "validate_and_sync_group_on_first_subscribe", validate_mock)
    config = _config()
    notify_admin = AsyncMock()

    await run_legacy_migration_if_configured(conn, config, notify_admin, AsyncMock(), MagicMock())

    validate_mock.assert_not_awaited()
    notify_admin.assert_awaited_once()
    assert storage.get_setting(conn, "legacy_migration_done_at") is None


@pytest.mark.asyncio
async def test_postpones_when_pdf_validation_fails(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    def fake_refresh(conn_, institute, *, notify_admin):
        link_id = storage.create_institute_link(
            conn_, institute_id=institute.id, pdf_url="https://example.com/a.pdf",
            group_names=["Б-Мв-101"], raw_label="Б-Мв-101", last_seen_at="t",
        )
        storage.create_group(conn_, institute_id=institute.id, institute_link_id=link_id, name="Б-Мв-101")

    monkeypatch.setattr(mod, "refresh_institute_catalog", fake_refresh)
    monkeypatch.setattr(mod, "validate_and_sync_group_on_first_subscribe", AsyncMock(return_value=False))
    config = _config()
    notify_admin = AsyncMock()

    other_institute_id = storage.create_institute(
        conn, name="Другой", catalog_url="https://other.example.com",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )
    other_link_id = storage.create_institute_link(
        conn, institute_id=other_institute_id, pdf_url="https://other.example.com/x.pdf",
        group_names=["Старая"], raw_label="Старая", last_seen_at="t",
    )
    old_group_id = storage.create_group(conn, institute_id=other_institute_id, institute_link_id=other_link_id, name="Старая")
    storage.upsert_member(conn, 777, old_group_id, joined_at="t")

    await run_legacy_migration_if_configured(conn, config, notify_admin, AsyncMock(), MagicMock())

    notify_admin.assert_awaited_once()
    assert storage.get_setting(conn, "legacy_migration_done_at") is None
    assert storage.get_member(conn, 777).group_id == old_group_id  # untouched — validation never succeeded


@pytest.mark.asyncio
async def test_successful_migration_reassigns_all_members_and_sets_flag_once(conn, monkeypatch):
    import schedule_bot.legacy_migration as mod

    def fake_refresh(conn_, institute, *, notify_admin):
        link_id = storage.create_institute_link(
            conn_, institute_id=institute.id, pdf_url="https://example.com/a.pdf",
            group_names=["Б-Мв-101"], raw_label="Б-Мв-101", last_seen_at="t",
        )
        storage.create_group(conn_, institute_id=institute.id, institute_link_id=link_id, name="Б-Мв-101")

    monkeypatch.setattr(mod, "refresh_institute_catalog", fake_refresh)
    monkeypatch.setattr(mod, "validate_and_sync_group_on_first_subscribe", AsyncMock(return_value=True))
    config = _config()
    notify_admin = AsyncMock()
    sync_fn = AsyncMock()
    reschedule = MagicMock()

    # Two pre-existing members, each in a DIFFERENT old group, with
    # distinct subscribed/joined_at — migration must move BOTH, and must
    # not disturb those fields.
    other_institute_id = storage.create_institute(
        conn, name="Старый", catalog_url="https://old.example.com",
        check_interval_minutes=60, send_time="09:00", tz_name="Europe/Moscow",
        catalog_refresh_interval_minutes=10080,
    )
    old_link_id = storage.create_institute_link(
        conn, institute_id=other_institute_id, pdf_url="https://old.example.com/x.pdf",
        group_names=["Старая-1", "Старая-2"], raw_label="Старая-1,Старая-2", last_seen_at="t",
    )
    old_group_1 = storage.create_group(conn, institute_id=other_institute_id, institute_link_id=old_link_id, name="Старая-1")
    old_group_2 = storage.create_group(conn, institute_id=other_institute_id, institute_link_id=old_link_id, name="Старая-2")
    storage.upsert_member(conn, 111, old_group_1, joined_at="2025-01-01T00:00:00+00:00", subscribed=True)
    storage.upsert_member(conn, 222, old_group_2, joined_at="2025-02-02T00:00:00+00:00", subscribed=False)

    await run_legacy_migration_if_configured(conn, config, notify_admin, sync_fn, reschedule)

    institute = storage.get_institute_by_catalog_url(conn, "https://example.com/institut")
    target_group = storage.get_group_by_name(conn, institute.id, "Б-Мв-101")

    member_1 = storage.get_member(conn, 111)
    member_2 = storage.get_member(conn, 222)
    assert member_1.group_id == target_group.id
    assert member_1.subscribed is True
    assert member_1.joined_at == "2025-01-01T00:00:00+00:00"
    assert member_2.group_id == target_group.id
    assert member_2.subscribed is False
    assert member_2.joined_at == "2025-02-02T00:00:00+00:00"

    assert storage.get_setting(conn, "legacy_migration_done_at") is not None
    notify_admin.assert_awaited_once()

    # Second call must be a pure no-op now that the flag is set.
    notify_admin.reset_mock()
    monkeypatch.setattr(mod, "refresh_institute_catalog", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run again")))
    await run_legacy_migration_if_configured(conn, config, notify_admin, sync_fn, reschedule)
    notify_admin.assert_not_awaited()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_legacy_migration.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.legacy_migration'`

- [ ] **Step 3: Implement**

Create `schedule_bot/legacy_migration.py`:

```python
import asyncio
import logging
from datetime import datetime, timezone

from schedule_bot import storage
from schedule_bot.group_commands import validate_and_sync_group_on_first_subscribe
from schedule_bot.group_links import refresh_institute_catalog

logger = logging.getLogger(__name__)

_DONE_SETTING_KEY = "legacy_migration_done_at"


async def run_legacy_migration_if_configured(
    conn, config, notify_admin, sync_institute_link_fn, reschedule_institute_jobs,
) -> None:
    """One-time migration of every existing `members` row onto a single
    target group, gated by three required env vars surfaced as
    `config.legacy_migration`. Runs at most once ever — guarded by the
    `legacy_migration_done_at` settings key, so a later manual
    /change_group by a migrated user is never reverted by a restart that
    still has the env vars set. Any failure along the way (catalog
    unreachable, target group missing, PDF validation failing) leaves
    the flag unset so the next restart retries from scratch."""
    legacy_migration = config.legacy_migration
    if legacy_migration is None:
        return
    if storage.get_setting(conn, _DONE_SETTING_KEY) is not None:
        return

    catalog_url, institute_name, group_name = legacy_migration

    institute = storage.get_institute_by_catalog_url(conn, catalog_url)
    if institute is None:
        institute_id = storage.create_institute(
            conn, name=institute_name, catalog_url=catalog_url,
            check_interval_minutes=config.default_check_interval_minutes,
            send_time=config.default_daily_send_time, tz_name=config.default_timezone,
            catalog_refresh_interval_minutes=config.default_catalog_refresh_interval_minutes,
        )
        institute = storage.get_institute(conn, institute_id)

    alerts: list[str] = []
    try:
        await asyncio.to_thread(refresh_institute_catalog, conn, institute, notify_admin=alerts.append)
    except Exception as error:
        logger.warning("legacy_migration: catalog scan failed for institute_id=%s: %s", institute.id, error)
        await notify_admin(
            f"Миграция легаси-пользователей отложена: каталог института «{institute.name}» недоступен: {error}"
        )
        return
    for text in alerts:
        await notify_admin(text)

    reschedule_institute_jobs(institute.id)

    group = storage.get_group_by_name(conn, institute.id, group_name)
    if group is None:
        logger.warning(
            "legacy_migration: group %r not found in institute_id=%s after catalog scan", group_name, institute.id
        )
        await notify_admin(
            f"Миграция легаси-пользователей отложена: группа «{group_name}» не найдена в каталоге института «{institute.name}»."
        )
        return

    ok = await validate_and_sync_group_on_first_subscribe(
        conn, group, config.pdf_cache_dir, notify_admin, sync_institute_link_fn,
    )
    if not ok:
        logger.warning("legacy_migration: group %r failed PDF validation, migration postponed", group_name)
        await notify_admin(
            f"Миграция легаси-пользователей отложена: группа «{group_name}» не подтвердилась по PDF."
        )
        return

    chat_ids = storage.list_all_member_chat_ids(conn)
    for chat_id in chat_ids:
        storage.reassign_member_group(conn, chat_id, group.id)

    storage.set_setting(conn, _DONE_SETTING_KEY, datetime.now(timezone.utc).isoformat())
    await notify_admin(
        f"Миграция легаси-пользователей выполнена: {len(chat_ids)} участников переключено в группу "
        f"«{group.name}» института «{institute.name}»."
    )
```

Note on the `fake_refresh` helpers used in the tests above: they are
plain (non-async, non-blocking) functions assigned directly as the
module-level `refresh_institute_catalog` name, so
`asyncio.to_thread(refresh_institute_catalog, ...)` still works —
`asyncio.to_thread` runs any sync callable, mocked or not.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_legacy_migration.py -v`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/legacy_migration.py tests/test_legacy_migration.py
git commit -m "feat: add one-time legacy user migration module"
```

---

### Task 6: Wire migration into `main.py`

**Files:**
- Modify: `main.py`
- Test: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: `legacy_migration.run_legacy_migration_if_configured` (Task 5); existing `build_application` locals `sync_institute_link_fn`, `reschedule_institute_jobs_cb`, `send_with_retry`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_main_wiring.py`:

```python
@pytest.mark.asyncio
async def test_post_init_runs_legacy_migration_when_configured(conn, config, monkeypatch):
    import main as main_module

    config.legacy_migration_catalog_url = "https://example.com/institut"
    config.legacy_migration_institute_name = "Институт агробизнеса"
    config.legacy_migration_group_name = "Б-Мв-101"

    _patch_week_formula(monkeypatch)
    called_with = {}

    async def fake_migration(conn_, config_, notify_admin, sync_fn, reschedule):
        called_with["conn"] = conn_
        called_with["config"] = config_
        called_with["notify_admin"] = notify_admin
        called_with["sync_fn"] = sync_fn
        called_with["reschedule"] = reschedule

    monkeypatch.setattr(main_module, "run_legacy_migration_if_configured", fake_migration)

    # application.bot is a real (frozen) ExtBot instance — TelegramObject
    # forbids setting instance attributes directly once frozen, so patch
    # the methods at the class level instead (monkeypatch auto-reverts).
    application = main_module.build_application(config, conn)
    monkeypatch.setattr(type(application.bot), "get_chat", AsyncMock(return_value=MagicMock(username=None, full_name=None)))
    monkeypatch.setattr(type(application.bot), "set_my_commands", AsyncMock(return_value=True))

    await application.post_init(application)

    assert called_with["conn"] is conn
    assert called_with["config"] is config
    assert called_with["sync_fn"] is application.bot_data["sync_institute_link_fn"]
    assert called_with["reschedule"] is application.bot_data["reschedule_institute_jobs"]

    application.bot_data["_scheduler"].shutdown(wait=False)


@pytest.mark.asyncio
async def test_post_init_skips_legacy_migration_when_not_configured(conn, config, monkeypatch):
    import main as main_module

    _patch_week_formula(monkeypatch)
    migration_mock = AsyncMock()
    monkeypatch.setattr(main_module, "run_legacy_migration_if_configured", migration_mock)

    application = main_module.build_application(config, conn)
    monkeypatch.setattr(type(application.bot), "get_chat", AsyncMock(return_value=MagicMock(username=None, full_name=None)))
    monkeypatch.setattr(type(application.bot), "set_my_commands", AsyncMock(return_value=True))

    await application.post_init(application)

    migration_mock.assert_awaited_once()  # still called — it's the function's own job to no-op

    application.bot_data["_scheduler"].shutdown(wait=False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py -k legacy_migration -v`
Expected: FAIL — `main_module.run_legacy_migration_if_configured` does
not exist yet (`AttributeError`), and `_post_init` never calls it.

- [ ] **Step 3: Implement**

In `main.py`, add the import alongside the other `schedule_bot.*`
imports (near `from schedule_bot.sync import sync_institute_link`):

```python
from schedule_bot.legacy_migration import run_legacy_migration_if_configured
```

In `build_application`'s `_post_init`, add the call as the last step,
after the existing `await _backfill_member_profiles(conn, app.bot)`
line:

```python
        await _backfill_member_profiles(conn, app.bot)

        async def _notify_admin(text: str) -> None:
            await send_with_retry(lambda: app.bot.send_message(chat_id=config.admin_chat_id, text=text))

        await run_legacy_migration_if_configured(
            conn, config, _notify_admin, sync_institute_link_fn, reschedule_institute_jobs_cb,
        )
```

This references `sync_institute_link_fn` and `reschedule_institute_jobs_cb`,
both defined later in the same `build_application` function body — that
is safe because `_post_init` is a closure that only runs when
`app.post_init(app)` is invoked (in the tests, explicitly; in production,
by `python-telegram-bot` during `run_polling()`), which is always after
`build_application` has finished executing and both names are bound.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/bin/python -m pytest tests/test_main_wiring.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the full suite**

Run: `./.venv/bin/python -m pytest -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main_wiring.py
git commit -m "feat: run legacy user migration from post_init on bot startup"
```

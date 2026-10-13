import json
import logging
import re
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urljoin

from schedule_bot import storage
from schedule_bot.browser_client import fetch_url

logger = logging.getLogger(__name__)

_PDF_LINK_RE = re.compile(
    r'<a\b[^>]*href=["\']([^"\']+\.pdf)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_GENERIC_LINK_RE = re.compile(
    r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_FORM_OF_STUDY_LABEL_RE = re.compile(r"форма обучения", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[\s\xa0]+")
_TRAILING_DIGITS = re.compile(r"\d+$")
_GROUP_NAME_RE = re.compile(r"^[А-ЯA-Zа-яa-z]+(-[А-ЯA-Zа-яa-z0-9]+)+$")
_COURSE_PREFIX_RE = re.compile(r"^\d+\s*курс\S*\s+", re.IGNORECASE)
_DATE_SUFFIX_RE = re.compile(r"\s+с\s+\d{1,2}\.\d{1,2}\.\d{2,4}$", re.IGNORECASE)


class SourcePageUnreachableError(Exception):
    pass


class GroupLinkNotFoundError(Exception):
    pass


def _normalize_label(raw_label: str) -> str:
    """Normalize whitespace in label: collapse all whitespace sequences
    (including non-breaking spaces) to single spaces and strip edges."""
    return _WHITESPACE_RE.sub(" ", raw_label).strip()


def expand_group_names(raw_label: str) -> list[str]:
    """Splits raw_label on commas (surrounding whitespace ignored). A
    fragment that is purely digits is prefixed with the previous full
    name's non-digit prefix in the same label ("Б-А-301,302,303" ->
    ["Б-А-301", "Б-А-302", "Б-А-303"]). A fragment containing letters is
    a standalone full name. If the very first fragment is purely
    digits (no previous name to borrow a prefix from), the whole label
    is invalid and skipped with a logged warning — this format isn't
    something this rule was calibrated for (see the "Known limitation"
    note in the design spec, section 2).

    Before splitting, a leading "<N> курс[...] " prefix ("1 курс ",
    "5 курс ") and a trailing " с <D>.<M>.<Y>" effective-date suffix
    (" с 12.10.2026") are stripped — some institutes' catalog pages
    wrap the actual comma-separated name list in both, and neither is
    part of any group's name."""
    raw_label = _COURSE_PREFIX_RE.sub("", raw_label)
    raw_label = _DATE_SUFFIX_RE.sub("", raw_label)
    fragments = [f.strip() for f in raw_label.split(",")]
    names: list[str] = []
    prefix: str | None = None
    for fragment in fragments:
        if not fragment:
            continue
        if fragment.isdigit():
            if prefix is None:
                logger.warning(
                    "group_links: raw_label %r starts with a bare digit fragment, no prefix to borrow — skipping", raw_label
                )
                return []
            names.append(f"{prefix}{fragment}")
        else:
            names.append(fragment)
            prefix = _TRAILING_DIGITS.sub("", fragment)
    return names


def list_pdf_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Returns [(pdf_url, raw_label), ...] for every <a href="*.pdf"> on
    html, without filtering by group name. raw_label is whitespace-
    normalized (collapsed, stripped) but NOT casefolded — case must
    survive to match PDF column headers later (see group_commands
    validation)."""
    results = []
    for href, inner_html in _PDF_LINK_RE.findall(html):
        visible_text = _TAG_RE.sub("", inner_html)
        results.append((urljoin(base_url, href), _normalize_label(visible_text)))
    return results


def list_form_of_study_links(html: str, base_url: str) -> list[tuple[str, str]]:
    """Returns [(url, label), ...] for every <a> on html whose visible
    text contains "форма обучения" (case-insensitive). The institute's
    catalog page links to up to three such subpages (Очная/Очно-заочная/
    Заочная form of study) and ONLY those subpages hold the actual PDF
    links — not every institute has all three, a form missing from the
    page's menu simply produces no entry here."""
    results = []
    for href, inner_html in _GENERIC_LINK_RE.findall(html):
        if href.lower().endswith(".pdf"):
            continue
        visible_text = _TAG_RE.sub("", inner_html)
        label = _normalize_label(visible_text)
        if _FORM_OF_STUDY_LABEL_RE.search(label):
            results.append((urljoin(base_url, href), label))
    return results


def resolve_pdf_link(html: str, group_name: str, base_url: str) -> str | None:
    """Find the first PDF link on `html` whose href or visible text
    contains `group_name` as a case-insensitive substring. One link can
    legitimately cover two groups at once (e.g. a file named
    "1 курс БЭ, БМ оз.pdf"), so this matches on substring, not equality,
    and returns the first match deterministically when several links
    would match different group names on the same page."""
    needle = group_name.casefold()
    for href, inner_html in _PDF_LINK_RE.findall(html):
        visible_text = _TAG_RE.sub("", inner_html)
        if needle in href.casefold() or needle in visible_text.casefold():
            return urljoin(base_url, href)
    return None


def fetch_source_page_html(source_page_url: str) -> str:
    try:
        response = fetch_url(source_page_url, timeout=15)
        response.raise_for_status()
    except Exception as error:
        raise SourcePageUnreachableError(str(error)) from error
    return response.text


def resolve_group_pdf_source(source_page_url: str, group_name: str) -> str:
    """Fetch `source_page_url` and resolve the PDF link for `group_name`.
    Raises SourcePageUnreachableError if the page itself can't be
    fetched, or GroupLinkNotFoundError if no matching link is found."""
    html = fetch_source_page_html(source_page_url)
    link = resolve_pdf_link(html, group_name, source_page_url)
    if link is None:
        raise GroupLinkNotFoundError(
            f"Не найдена ссылка на расписание группы {group_name!r} на странице {source_page_url}"
        )
    return link


def looks_like_group_name(name: str) -> bool:
    """Cheap heuristic filter for the "Letter(s)-Letter(s)[-Letter(s) or
    Digits]" shape — at least one hyphen-joined segment after the first
    ("Б-А-301", "Б-Мв-101", "Б-ТОП", "М-УК-О"). The final segment need
    not be numeric: some institutes code groups purely with letters.
    Filters out obvious catalog noise (orders, guidelines, free text)
    at name-expansion time, before a `groups` row is ever created. Does
    NOT replace PDF-based validation (group_commands.py); see design
    spec section 1.5/2."""
    return bool(_GROUP_NAME_RE.match(name.strip()))


def _resolve_link_identity(snapshot_links, pdf_url: str, group_names: list[str]):
    """Two-phase match per design spec section 1.2. `snapshot_links` is a
    frozen list of InstituteLinkRow taken BEFORE this scan's loop started
    — matching must never consider links created/updated earlier in the
    SAME scan, or two distinct PDF links that happen to list the same
    group name in one scan would collapse into each other (see the
    "conflict" test: two different brand-new links both claiming
    "Б-А-301" in a single scan must stay two separate links, not get
    merged via the name-set phase). Returns the matched InstituteLinkRow,
    or None if this is a brand-new link."""
    for link in snapshot_links:
        if link.pdf_url == pdf_url:
            return link

    candidate_names = set(group_names)
    if not candidate_names:
        # An empty name set matches nothing by phase 2 — `set() == set()`
        # is vacuously True for ANY two links with no valid group names,
        # which would otherwise merge unrelated catalog noise (e.g. two
        # different "Приказ..." links across two scans) into one row via
        # a meaningless coincidence. Noise links can only ever be
        # re-identified by pdf_url (phase 1).
        return None
    for link in snapshot_links:
        if set(json.loads(link.group_names_json)) == candidate_names:
            return link
    return None


def _upsert_groups_for_link(
    conn, institute_id: int, link_id: int, group_names: list[str], confirmed_group_ids: set[int]
) -> None:
    for name in group_names:
        existing = storage.get_group_by_name(conn, institute_id, name)
        if existing is None:
            group_id = storage.create_group(conn, institute_id=institute_id, institute_link_id=link_id, name=name)
            confirmed_group_ids.add(group_id)
            continue
        if existing.institute_link_id != link_id:
            # Conflict rule (spec 1.4): only reassign if this group hasn't
            # already been seen under a DIFFERENT link earlier in THIS scan.
            if existing.id not in confirmed_group_ids:
                storage.update_group_institute_link_id(conn, existing.id, link_id)
            else:
                logger.warning(
                    "group_links: group %r appears under two links in one scan; keeping its first binding", name
                )
        confirmed_group_ids.add(existing.id)


def refresh_institute_catalog(conn, institute, *, notify_admin: Callable[[str], None]) -> None:
    """Scan `institute.catalog_url` two levels deep: first find the
    "форма обучения" (form of study) subpage links on it via
    `list_form_of_study_links` (up to three — Очная/Очно-заочная/Заочная,
    not every institute has all three), then fetch each reachable one and
    collect its PDF links via `list_pdf_links`. The combined PDF links
    are matched against existing `institute_links` rows (two-phase
    identity: pdf_url, then the expanded group-name set), creating/
    updating `institute_links` and `groups` rows accordingly, and marking
    any previously-live link that wasn't seen in this scan as stale.
    Never downloads/parses the PDFs themselves (that's the sync
    pipeline's job) and never sets `groups.is_verified` (that's done at
    subscribe time).

    If the catalog page has 0 form-of-study links, or every reachable
    form-of-study page together has 0 PDF links, this must NOT touch the
    DB at all — just log and send exactly one admin alert, then return.
    If only SOME form-of-study pages are unreachable this cycle (the
    others succeed), stale-marking is skipped entirely for this scan —
    a transient fetch failure of one form's page must never be read as
    "that form's links are gone from the catalog".
    """
    html = fetch_source_page_html(institute.catalog_url)
    form_links = list_form_of_study_links(html, institute.catalog_url)

    if not form_links:
        logger.warning("group_links: catalog for institute %r returned 0 form-of-study links", institute.name)
        notify_admin(f"Каталог института {institute.name} (id={institute.id}) вернул 0 ссылок на страницы форм обучения")
        return

    raw_entries: list[tuple[str, str]] = []
    scan_is_partial = False
    unreachable_form_count = 0
    for form_url, form_label in form_links:
        try:
            form_html = fetch_source_page_html(form_url)
        except SourcePageUnreachableError as error:
            logger.warning("group_links: form-of-study page %r (%s) unreachable: %s", form_label, form_url, error)
            scan_is_partial = True
            unreachable_form_count += 1
            continue
        raw_entries.extend(list_pdf_links(form_html, form_url))

    if not raw_entries:
        logger.warning("group_links: catalog for institute %r returned 0 PDF links across all form-of-study pages", institute.name)
        if unreachable_form_count == len(form_links):
            notify_admin(
                f"Каталог института {institute.name} (id={institute.id}) вернул 0 ссылок — "
                f"все {unreachable_form_count} страниц(ы) форм обучения недоступны"
            )
        else:
            notify_admin(f"Каталог института {institute.name} (id={institute.id}) вернул 0 ссылок")
        return

    # Dedup same pdf_url appearing under two different <a> tags in one scan.
    labels_by_url: dict[str, list[str]] = {}
    for pdf_url, raw_label in raw_entries:
        labels_by_url.setdefault(pdf_url, []).append(raw_label)

    confirmed_link_ids: list[int] = []
    now = datetime.now(timezone.utc).isoformat()
    seen_group_ids_this_scan: set[int] = set()
    # Snapshot taken once, before any mutation this scan — see
    # _resolve_link_identity's docstring for why this must not be a live
    # query re-run inside the loop.
    pre_scan_links = storage.list_institute_links(conn, institute.id)

    for pdf_url, raw_labels in labels_by_url.items():
        combined_raw_label = ", ".join(raw_labels)
        expanded = [
            name for raw_label in raw_labels for name in expand_group_names(raw_label) if looks_like_group_name(name)
        ]

        matched = _resolve_link_identity(pre_scan_links, pdf_url, expanded)
        if matched is None:
            link_id = storage.create_institute_link(
                conn, institute_id=institute.id, pdf_url=pdf_url,
                group_names=expanded, raw_label=combined_raw_label, last_seen_at=now,
            )
        elif matched.pdf_url != pdf_url:
            storage.update_institute_link_pdf_url(conn, matched.id, pdf_url)
            storage.update_institute_link_group_names(
                conn, matched.id, group_names=expanded, raw_label=combined_raw_label, last_seen_at=now
            )
            link_id = matched.id
        else:
            storage.update_institute_link_group_names(
                conn, matched.id, group_names=expanded, raw_label=combined_raw_label, last_seen_at=now
            )
            link_id = matched.id

        confirmed_link_ids.append(link_id)
        _upsert_groups_for_link(conn, institute.id, link_id, expanded, seen_group_ids_this_scan)

    storage.set_institute_last_catalog_sync_at(conn, institute.id, now)

    if scan_is_partial:
        # One or more form-of-study pages were unreachable this cycle, so
        # confirmed_link_ids is NOT a complete picture of every link that
        # is still live in the catalog — treating it as one would mark
        # links on the unreachable form(s) stale (and stop their sync)
        # over a transient site hiccup, not an actual catalog change.
        # Skip stale-marking entirely; it resumes on a scan where every
        # form-of-study page was reachable.
        logger.warning(
            "group_links: catalog scan for institute %r was partial (some form-of-study pages unreachable) — skipping stale-marking",
            institute.name,
        )
        return

    previously_live_link_ids = {
        link.id for link in storage.list_institute_links(conn, institute.id, include_stale=False)
    }
    newly_stale_ids = previously_live_link_ids - set(confirmed_link_ids)

    storage.mark_stale_links_not_in(conn, institute.id, confirmed_link_ids)

    for link_id in newly_stale_ids:
        affected_subscriber_count = sum(
            len(storage.list_group_members(conn, group.id, subscribed_only=False))
            for group in storage.list_groups_for_link(conn, link_id)
        )
        if affected_subscriber_count > 0:
            notify_admin(
                f"Ссылка института {institute.name} (id={institute.id}) пропала из каталога, "
                f"подписчиков затронуто: {affected_subscriber_count}"
            )

import logging
import re
from urllib.parse import urljoin

from schedule_bot.browser_client import fetch_url

logger = logging.getLogger(__name__)

_PDF_LINK_RE = re.compile(
    r'<a\b[^>]*href=["\']([^"\']+\.pdf)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"[\s\xa0]+")
_TRAILING_DIGITS = re.compile(r"\d+$")


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
    note in the design spec, section 2)."""
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

import re
from urllib.parse import urljoin

from schedule_bot.browser_client import fetch_url

_PDF_LINK_RE = re.compile(
    r'<a\b[^>]*href=["\']([^"\']+\.pdf)["\'][^>]*>(.*?)</a>', re.IGNORECASE | re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")


class SourcePageUnreachableError(Exception):
    pass


class GroupLinkNotFoundError(Exception):
    pass


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

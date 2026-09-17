import random
import time
from urllib.parse import urlsplit

# A full, internally-consistent Chrome-on-Windows header set (order and
# values a real navigation actually sends) — a bare User-Agent with none
# of Sec-Fetch-*/Sec-Ch-Ua-* is itself a bot fingerprint to anti-bot
# systems that check header *combinations*, not just User-Agent.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}

# curl_cffi's "impersonate" replicates a real Chrome's TLS ClientHello
# (JA3) and negotiates HTTP/2, which `requests`/urllib3 never do — that
# fingerprint is checked before a single HTTP header is even read, so
# matching headers alone would still identify this as non-browser traffic.
_IMPERSONATE = "chrome124"


def site_root(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def fetch_url(url: str, *, timeout: float, referer: str | None = None):
    """GET `url` through a session configured to look like a real Chrome
    browser. When `referer` is given, first "visits" it (picking up
    cookies, like a browser would) and pauses briefly before following
    the link — a cold request straight to a file with no prior page visit
    and no delay is itself a bot signal."""
    from curl_cffi import requests as curl_requests

    session = curl_requests.Session(impersonate=_IMPERSONATE)
    headers = dict(BROWSER_HEADERS)
    if referer:
        session.get(referer, headers=headers, timeout=timeout)
        time.sleep(random.uniform(0.8, 2.5))
        headers = {**headers, "Referer": referer, "Sec-Fetch-Site": "same-origin"}
    return session.get(url, headers=headers, timeout=timeout)

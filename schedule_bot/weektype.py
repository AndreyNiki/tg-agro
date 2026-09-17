import re
from dataclasses import dataclass
from datetime import date

WEEK_UPPER = "Верхняя"
WEEK_LOWER = "Нижняя"

_BASE_DATE_RE = re.compile(r"new Date\(year,\s*(\d+),\s*(\d+)\)")
_PARITY_RE = re.compile(r"(?m)^(?!\s*//).*?weekIndex\s*%\s*2\s*===\s*0\s*\?\s*'([^']+)'\s*:\s*'([^']+)'")


@dataclass
class WeekFormula:
    base_month: int  # 1-indexed calendar month
    base_day: int
    even_index_is_upper: bool

    def compute(self, on_date: date) -> str:
        base = date(on_date.year, self.base_month, self.base_day)
        week_index = (on_date - base).days // 7
        is_even = week_index % 2 == 0
        return WEEK_UPPER if is_even == self.even_index_is_upper else WEEK_LOWER


def parse_week_formula(html: str) -> WeekFormula:
    base_match = _BASE_DATE_RE.search(html)
    parity_match = _PARITY_RE.search(html)
    if not base_match or not parity_match:
        raise ValueError("Week type formula pattern not found in page HTML")
    js_month, day = int(base_match.group(1)), int(base_match.group(2))
    even_value = parity_match.group(1)
    return WeekFormula(
        base_month=js_month + 1,
        base_day=day,
        even_index_is_upper=(even_value == WEEK_UPPER),
    )


def fetch_week_formula(url: str) -> WeekFormula:
    from schedule_bot.browser_client import fetch_url

    response = fetch_url(url, timeout=15)
    response.raise_for_status()
    return parse_week_formula(response.text)


_SETTING_BASE_MONTH = "weekformula_base_month"
_SETTING_BASE_DAY = "weekformula_base_day"
_SETTING_EVEN_UPPER = "weekformula_even_upper"


def save_week_formula(conn, formula: WeekFormula) -> None:
    """Persist a WeekFormula to settings so it survives process restarts
    even if the next scheduled/startup fetch of the institute's page fails."""
    from schedule_bot import storage

    storage.set_setting(conn, _SETTING_BASE_MONTH, str(formula.base_month))
    storage.set_setting(conn, _SETTING_BASE_DAY, str(formula.base_day))
    storage.set_setting(conn, _SETTING_EVEN_UPPER, "1" if formula.even_index_is_upper else "0")


def load_week_formula(conn) -> WeekFormula | None:
    """Load a previously-persisted WeekFormula from settings, or None if
    any of the three keys is missing (never successfully persisted)."""
    from schedule_bot import storage

    base_month = storage.get_setting(conn, _SETTING_BASE_MONTH)
    base_day = storage.get_setting(conn, _SETTING_BASE_DAY)
    even_upper = storage.get_setting(conn, _SETTING_EVEN_UPPER)
    if base_month is None or base_day is None or even_upper is None:
        return None
    return WeekFormula(
        base_month=int(base_month),
        base_day=int(base_day),
        even_index_is_upper=(even_upper == "1"),
    )

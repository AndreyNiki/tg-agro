import sqlite3
from datetime import date
from pathlib import Path
import pytest
from schedule_bot import storage
from schedule_bot.weektype import (
    WEEK_UPPER,
    WEEK_LOWER,
    WeekFormula,
    load_week_formula,
    parse_week_formula,
    save_week_formula,
)

FIXTURE_HTML = (Path(__file__).parent / "fixtures" / "weektype_page.html").read_text(encoding="utf-8")


def test_parse_week_formula_extracts_base_date_and_parity():
    formula = parse_week_formula(FIXTURE_HTML)
    assert formula.base_month == 1
    assert formula.base_day == 5
    assert formula.even_index_is_upper is True


def test_parse_week_formula_raises_when_pattern_missing():
    with pytest.raises(ValueError):
        parse_week_formula("<html><body>no script here</body></html>")


@pytest.mark.parametrize(
    "on_date,expected",
    [
        (date(2026, 1, 5), WEEK_UPPER),   # week_index 0, even -> upper
        (date(2026, 1, 11), WEEK_UPPER),  # still within week 0 (days 0-6)
        (date(2026, 1, 12), WEEK_LOWER),  # week_index 1, odd -> lower
        (date(2026, 9, 16), WEEK_UPPER),  # week_index 36, even -> upper (hand-verified)
    ],
)
def test_week_formula_compute(on_date, expected):
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    assert formula.compute(on_date) == expected


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def test_load_week_formula_returns_none_when_nothing_persisted(conn):
    assert load_week_formula(conn) is None


def test_save_and_load_week_formula_round_trips(conn):
    formula = WeekFormula(base_month=3, base_day=17, even_index_is_upper=False)
    save_week_formula(conn, formula)
    assert load_week_formula(conn) == formula

# Telegram Schedule Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Telegram bot that parses the group's class schedule from a PDF, tracks the "Верхняя/Нижняя" week type by a client-side formula scraped from the institute's site, sends a daily schedule message (with a cropped screenshot of that day's row band), checks hourly for schedule changes and broadcasts them automatically, and exposes `/schedule` (day-picker) plus admin commands for configuration and manual control.

**Architecture:** A flat `schedule_bot/` package with one module per concern (config, sqlite storage, week-type formula, PDF parsing, diffing, screenshot rendering, text formatting, sync orchestration, APScheduler wiring, Telegram handlers). `pdf_parser.py` is split into a thin pdfplumber-extraction layer and a pure, geometry-driven `build_schedule` function so the tricky table logic (day/time forward-fill, upper/lower week split, cross-group merged-cell detection) is unit-testable without needing real PDF files for every case. All settings that must be changeable without a redeploy live in a `settings` SQLite table, seeded from environment variables on first run.

**Tech Stack:** Python 3.12+, `python-telegram-bot` (async, polling), `pdfplumber`, `PyMuPDF` (`fitz`), `APScheduler`, `requests`, stdlib `sqlite3`, `pytest` + `pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-09-16-schedule-bot-design.md`

## Global Constraints

- Target group for this iteration: `Б-Мв-101` (configurable via `settings.group_name`, default from `GROUP_NAME` env var).
- `pdf_source` setting accepts either an `http(s)://` URL or a local filesystem path — selected by checking the string prefix, no separate flag.
- No confirm/reject workflow: any detected schedule diff is applied immediately and broadcast automatically. There is a separate admin-only `/alert <text>` command for manual ad-hoc broadcasts.
- Upper (first) row within a split table cell = Верхняя week; lower (second) row = Нижняя week — confirmed convention, not stated anywhere in the PDF itself.
- A cell in an earlier group's column counts as a merged (shared) lesson for the target group only when its right edge (`x1`) reaches the table's overall right edge (within a small epsilon) — not merely its own column's right edge.
- `daily_send_time` and `check_interval_minutes` are both runtime-configurable via admin commands and must reschedule the corresponding APScheduler job immediately, without a process restart.
- `subscribers` and `schedule_versions` both carry a `group_name` column from day one, even though only one group is in active use — this is groundwork for future multi-group support, not built out now.
- Real, validated fixtures ship in the repo: `tests/fixtures/schedule_sample.pdf` (the actual downloaded PDF, already hand-verified row by row) and a `tests/fixtures/weektype_page.html` snippet (created in Task 3) containing the real `<script>` block from the institute's site.

---

## Task 1: Project scaffolding & config module

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `schedule_bot/__init__.py`
- Create: `schedule_bot/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `schedule_bot.config.Config` (dataclass), `schedule_bot.config.load_config(env: Mapping[str, str]) -> Config`

- [ ] **Step 1: Create the package and dependency files**

`requirements.txt`:
```
python-telegram-bot==21.6
pdfplumber==0.11.4
pymupdf==1.24.11
APScheduler==3.10.4
requests==2.32.3
pytest==8.3.3
pytest-asyncio==0.24.0
```

`.env.example`:
```
TELEGRAM_BOT_TOKEN=
ADMIN_CHAT_ID=
DB_PATH=./data/schedule.db
PDF_CACHE_DIR=./data/pdf_cache
PDF_SOURCE=https://www.vavilovsar.ru/files/pages/41427/1789471639_1%20%D0%BA%D1%83%D1%80%D1%81%20%D0%91%D0%AD,%20%D0%91%D0%9C%20%D0%BE%D0%B7.pdf
GROUP_NAME=Б-Мв-101
WEEKTYPE_URL=https://www.vavilovsar.ru/ucheba/raspisanie-zanyatii
DAILY_SEND_TIME=09:00
CHECK_INTERVAL_MINUTES=60
TIMEZONE=Europe/Moscow
```

`schedule_bot/__init__.py`: empty file.

- [ ] **Step 2: Write the failing test for config loading**

```python
# tests/test_config.py
import pytest
from schedule_bot.config import load_config


def test_load_config_reads_required_and_defaults():
    env = {
        "TELEGRAM_BOT_TOKEN": "abc123",
        "ADMIN_CHAT_ID": "42",
        "PDF_SOURCE": "https://example.com/schedule.pdf",
    }
    config = load_config(env)
    assert config.telegram_token == "abc123"
    assert config.admin_chat_id == 42
    assert config.default_pdf_source == "https://example.com/schedule.pdf"
    assert config.db_path == "./data/schedule.db"
    assert config.pdf_cache_dir == "./data/pdf_cache"
    assert config.default_group_name == "Б-Мв-101"
    assert config.default_weektype_url == "https://www.vavilovsar.ru/ucheba/raspisanie-zanyatii"
    assert config.default_daily_send_time == "09:00"
    assert config.default_check_interval_minutes == 60
    assert config.default_timezone == "Europe/Moscow"


def test_load_config_raises_on_missing_required_var():
    with pytest.raises(ValueError):
        load_config({"ADMIN_CHAT_ID": "42", "PDF_SOURCE": "x"})
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.config'`

- [ ] **Step 4: Implement `schedule_bot/config.py`**

```python
from dataclasses import dataclass
from typing import Mapping


@dataclass
class Config:
    telegram_token: str
    admin_chat_id: int
    db_path: str
    pdf_cache_dir: str
    default_pdf_source: str
    default_group_name: str
    default_weektype_url: str
    default_daily_send_time: str
    default_check_interval_minutes: int
    default_timezone: str


def load_config(env: Mapping[str, str]) -> Config:
    def require(key: str) -> str:
        value = env.get(key)
        if not value:
            raise ValueError(f"Missing required env var: {key}")
        return value

    return Config(
        telegram_token=require("TELEGRAM_BOT_TOKEN"),
        admin_chat_id=int(require("ADMIN_CHAT_ID")),
        pdf_cache_dir=env.get("PDF_CACHE_DIR", "./data/pdf_cache"),
        db_path=env.get("DB_PATH", "./data/schedule.db"),
        default_pdf_source=require("PDF_SOURCE"),
        default_group_name=env.get("GROUP_NAME", "Б-Мв-101"),
        default_weektype_url=env.get(
            "WEEKTYPE_URL", "https://www.vavilovsar.ru/ucheba/raspisanie-zanyatii"
        ),
        default_daily_send_time=env.get("DAILY_SEND_TIME", "09:00"),
        default_check_interval_minutes=int(env.get("CHECK_INTERVAL_MINUTES", "60")),
        default_timezone=env.get("TIMEZONE", "Europe/Moscow"),
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Commit**

```bash
git add requirements.txt .env.example schedule_bot/__init__.py schedule_bot/config.py tests/test_config.py
git commit -m "feat: add project scaffolding and config loading"
```

---

## Task 2: SQLite storage layer

**Files:**
- Create: `schedule_bot/storage.py`
- Test: `tests/test_storage.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (uses stdlib `sqlite3` directly)
- Produces:
  - `schedule_bot.storage.init_db(conn: sqlite3.Connection) -> None`
  - `schedule_bot.storage.ensure_default_settings(conn, defaults: dict[str, str]) -> None`
  - `schedule_bot.storage.get_setting(conn, key: str) -> str | None`
  - `schedule_bot.storage.set_setting(conn, key: str, value: str) -> None`
  - `schedule_bot.storage.add_subscriber(conn, chat_id: int, group_name: str, joined_at: str) -> None`
  - `schedule_bot.storage.remove_subscriber(conn, chat_id: int) -> None`
  - `schedule_bot.storage.list_subscribers(conn, group_name: str) -> list[int]`
  - `schedule_bot.storage.ScheduleVersionRow` (dataclass: `id, group_name, payload_json, pdf_cache_path, parsed_at, status`)
  - `schedule_bot.storage.get_active_version(conn, group_name: str) -> ScheduleVersionRow | None`
  - `schedule_bot.storage.insert_new_version(conn, group_name: str, payload_json: str, pdf_cache_path: str, parsed_at: str) -> int`

- [ ] **Step 1: Write failing tests for settings and subscribers**

```python
# tests/test_storage.py
import sqlite3
import pytest
from schedule_bot import storage


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    yield connection
    connection.close()


def test_ensure_default_settings_only_fills_missing_keys(conn):
    storage.set_setting(conn, "group_name", "Б-Мв-101")
    storage.ensure_default_settings(conn, {"group_name": "SHOULD_NOT_OVERWRITE", "daily_send_time": "09:00"})
    assert storage.get_setting(conn, "group_name") == "Б-Мв-101"
    assert storage.get_setting(conn, "daily_send_time") == "09:00"


def test_get_setting_returns_none_for_missing_key(conn):
    assert storage.get_setting(conn, "nope") is None


def test_set_setting_overwrites_existing_value(conn):
    storage.set_setting(conn, "check_interval_minutes", "60")
    storage.set_setting(conn, "check_interval_minutes", "30")
    assert storage.get_setting(conn, "check_interval_minutes") == "30"


def test_subscriber_lifecycle(conn):
    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    storage.add_subscriber(conn, 222, "Б-Мв-101", "2026-09-16T00:00:00")
    storage.add_subscriber(conn, 333, "OTHER-GROUP", "2026-09-16T00:00:00")
    assert sorted(storage.list_subscribers(conn, "Б-Мв-101")) == [111, 222]
    storage.remove_subscriber(conn, 111)
    assert storage.list_subscribers(conn, "Б-Мв-101") == [222]


def test_add_subscriber_is_idempotent(conn):
    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    assert storage.list_subscribers(conn, "Б-Мв-101") == [111]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_storage.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.storage'`

- [ ] **Step 3: Implement settings and subscriber functions in `schedule_bot/storage.py`**

```python
import sqlite3


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS subscribers (
            chat_id INTEGER PRIMARY KEY,
            group_name TEXT NOT NULL,
            joined_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS schedule_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            group_name TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            pdf_cache_path TEXT NOT NULL,
            parsed_at TEXT NOT NULL,
            status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        """
    )
    conn.commit()


def ensure_default_settings(conn: sqlite3.Connection, defaults: dict[str, str]) -> None:
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value)
        )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def add_subscriber(conn: sqlite3.Connection, chat_id: int, group_name: str, joined_at: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO subscribers (chat_id, group_name, joined_at) VALUES (?, ?, ?)",
        (chat_id, group_name, joined_at),
    )
    conn.commit()


def remove_subscriber(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute("DELETE FROM subscribers WHERE chat_id = ?", (chat_id,))
    conn.commit()


def list_subscribers(conn: sqlite3.Connection, group_name: str) -> list[int]:
    rows = conn.execute(
        "SELECT chat_id FROM subscribers WHERE group_name = ?", (group_name,)
    ).fetchall()
    return [row[0] for row in rows]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_storage.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Write failing tests for schedule versions**

```python
# append to tests/test_storage.py

def test_get_active_version_returns_none_when_empty(conn):
    assert storage.get_active_version(conn, "Б-Мв-101") is None


def test_insert_new_version_becomes_active_and_supersedes_old(conn):
    first_id = storage.insert_new_version(
        conn, "Б-Мв-101", '{"a": 1}', "/data/pdf_cache/v1.pdf", "2026-09-01T09:00:00"
    )
    second_id = storage.insert_new_version(
        conn, "Б-Мв-101", '{"a": 2}', "/data/pdf_cache/v2.pdf", "2026-09-08T09:00:00"
    )
    active = storage.get_active_version(conn, "Б-Мв-101")
    assert active.id == second_id
    assert active.payload_json == '{"a": 2}'
    assert active.status == "active"

    all_rows = conn.execute(
        "SELECT id, status FROM schedule_versions WHERE group_name = ? ORDER BY id", ("Б-Мв-101",)
    ).fetchall()
    assert all_rows == [(first_id, "superseded"), (second_id, "active")]


def test_versions_are_scoped_by_group_name(conn):
    storage.insert_new_version(conn, "Б-Мв-101", '{"a": 1}', "/v1.pdf", "2026-09-01T09:00:00")
    storage.insert_new_version(conn, "OTHER-GROUP", '{"b": 1}', "/v2.pdf", "2026-09-01T09:00:00")
    active = storage.get_active_version(conn, "Б-Мв-101")
    assert active.payload_json == '{"a": 1}'
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest tests/test_storage.py -v`
Expected: FAIL with `AttributeError: module 'schedule_bot.storage' has no attribute 'get_active_version'`

- [ ] **Step 7: Implement schedule version functions**

```python
# append to schedule_bot/storage.py
from dataclasses import dataclass


@dataclass
class ScheduleVersionRow:
    id: int
    group_name: str
    payload_json: str
    pdf_cache_path: str
    parsed_at: str
    status: str


def get_active_version(conn: sqlite3.Connection, group_name: str) -> ScheduleVersionRow | None:
    row = conn.execute(
        "SELECT id, group_name, payload_json, pdf_cache_path, parsed_at, status "
        "FROM schedule_versions WHERE group_name = ? AND status = 'active'",
        (group_name,),
    ).fetchone()
    return ScheduleVersionRow(*row) if row else None


def insert_new_version(
    conn: sqlite3.Connection,
    group_name: str,
    payload_json: str,
    pdf_cache_path: str,
    parsed_at: str,
) -> int:
    conn.execute(
        "UPDATE schedule_versions SET status = 'superseded' WHERE group_name = ? AND status = 'active'",
        (group_name,),
    )
    cursor = conn.execute(
        "INSERT INTO schedule_versions (group_name, payload_json, pdf_cache_path, parsed_at, status) "
        "VALUES (?, ?, ?, ?, 'active')",
        (group_name, payload_json, pdf_cache_path, parsed_at),
    )
    conn.commit()
    return cursor.lastrowid
```

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest tests/test_storage.py -v`
Expected: PASS (8 tests)

- [ ] **Step 9: Commit**

```bash
git add schedule_bot/storage.py tests/test_storage.py
git commit -m "feat: add sqlite storage layer for settings, subscribers, schedule versions"
```

---

## Task 3: Week-type formula parsing and computation

**Files:**
- Create: `schedule_bot/weektype.py`
- Create: `tests/fixtures/weektype_page.html`
- Test: `tests/test_weektype.py`

**Interfaces:**
- Produces:
  - `schedule_bot.weektype.WEEK_UPPER = "Верхняя"`, `schedule_bot.weektype.WEEK_LOWER = "Нижняя"`
  - `schedule_bot.weektype.WeekFormula` (dataclass: `base_month: int, base_day: int, even_index_is_upper: bool`, method `compute(self, on_date: datetime.date) -> str`)
  - `schedule_bot.weektype.parse_week_formula(html: str) -> WeekFormula` (raises `ValueError` if pattern not found)
  - `schedule_bot.weektype.fetch_week_formula(url: str) -> WeekFormula`

- [ ] **Step 1: Create the HTML fixture with the real script block**

`tests/fixtures/weektype_page.html` (this is the exact, verified snippet from `https://www.vavilovsar.ru/ucheba/raspisanie-zanyatii`):

```html
<html>
<body>
<table>
<tr><td width="150">
<div id="calendar"></div>

<script>
const today = new Date();
const year = today.getFullYear();

// 5 января — НИЖНЯЯ неделя
const baseDate = new Date(year, 0, 5);

// разница в днях
const diffDays = Math.floor(
  (today - baseDate) / (1000 * 60 * 60 * 24)
);

// номер недели от опорной даты
const weekIndex = Math.floor(diffDays / 7);

// тип недели
//const weekType = weekIndex % 2 === 0 ? 'Нижняя' : 'Верхняя';
const weekType = weekIndex % 2 === 0 ? 'Верхняя' : 'Нижняя';

// названия
const days = ['Воскресенье','Понедельник','Вторник','Среда','Четверг','Пятница','Суббота'];
const months = ['Январь','Февраль','Март','Апрель','Май','Июнь','Июль','Август','Сентябрь','Октябрь','Ноябрь','Декабрь'];

document.getElementById('calendar').innerHTML = `
  <table width="150" height="250" border="2">
    <tr><td align="center">${days[today.getDay()]}</td></tr>
    <tr><td align="center">${months[today.getMonth()]}</td></tr>
    <tr>
      <td align="center">
        <div style="font-size:32px">${today.getDate()}</div>
        <div style="color:blue">${weekType} неделя</div>
      </td>
    </tr>
    <tr><td align="center">${year}</td></tr>
  </table>
`;
</script>
</td></tr>
</table>
</body>
</html>
```

- [ ] **Step 2: Write the failing tests**

```python
# tests/test_weektype.py
from datetime import date
from pathlib import Path
import pytest
from schedule_bot.weektype import WEEK_UPPER, WEEK_LOWER, WeekFormula, parse_week_formula

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
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/test_weektype.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.weektype'`

- [ ] **Step 4: Implement `schedule_bot/weektype.py`**

```python
import re
from dataclasses import dataclass
from datetime import date

WEEK_UPPER = "Верхняя"
WEEK_LOWER = "Нижняя"

_BASE_DATE_RE = re.compile(r"new Date\(year,\s*(\d+),\s*(\d+)\)")
_PARITY_RE = re.compile(r"weekIndex\s*%\s*2\s*===\s*0\s*\?\s*'([^']+)'\s*:\s*'([^']+)'")


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
    import requests

    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return parse_week_formula(response.text)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_weektype.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add schedule_bot/weektype.py tests/fixtures/weektype_page.html tests/test_weektype.py
git commit -m "feat: parse week-type formula from institute site script"
```

---

## Task 4: PDF raw table extraction

**Files:**
- Create: `schedule_bot/pdf_parser.py` (extraction portion only — the schedule-building portion is Task 5)
- Test: `tests/test_pdf_parser_extraction.py`
- Fixture already present: `tests/fixtures/schedule_sample.pdf` (real, downloaded, hand-verified schedule PDF)

**Interfaces:**
- Produces:
  - `schedule_bot.pdf_parser.Cell` (dataclass: `text: str | None, x0: float, x1: float`)
  - `schedule_bot.pdf_parser.RawRow` (dataclass: `day_marker: bool, time_label: str | None, y0: float, y1: float, cells: list[Cell | None]`)
  - `schedule_bot.pdf_parser.extract_raw_rows(source: str) -> tuple[list[RawRow], tuple[float, float, float, float]]` (rows, table bbox `(x0, y0, x1, y1)`)
  - `schedule_bot.pdf_parser.find_group_column_index(rows: list[RawRow], group_name: str) -> tuple[int, int]` (header row index, column index)
  - `schedule_bot.pdf_parser._load_pdf_bytes(source: str) -> bytes` (internal helper: local path vs URL)

- [ ] **Step 1: Write the failing tests against the real fixture**

These assertions were hand-derived by inspecting `tests/fixtures/schedule_sample.pdf` directly with `pdfplumber` (table has 41 rows; row 1 is the header with group names; row 4's group-2 column spans x0≈443.4 to x1≈822.6; the table's overall right edge is ≈822.6).

```python
# tests/test_pdf_parser_extraction.py
from pathlib import Path
import pytest
from schedule_bot.pdf_parser import extract_raw_rows, find_group_column_index

FIXTURE_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


def test_extract_raw_rows_reads_local_path():
    rows, table_bbox = extract_raw_rows(FIXTURE_PATH)
    assert len(rows) == 41
    assert table_bbox[2] == pytest.approx(822.5956357142857, abs=0.5)


def test_extract_raw_rows_header_row_has_group_columns():
    rows, _ = extract_raw_rows(FIXTURE_PATH)
    header = rows[1]
    header_texts = [cell.text if cell else None for cell in header.cells]
    assert "Б-Эв-101" in header_texts
    assert "Б-Мв-101" in header_texts


def test_extract_raw_rows_day_marker_rows_match_known_positions():
    rows, _ = extract_raw_rows(FIXTURE_PATH)
    day_marker_indexes = [i for i, row in enumerate(rows) if row.day_marker]
    # Title row (0), header row (1, group names act as a "marker" too because
    # column 0 is non-empty there), the 6 real day-start rows, and the footer
    # row (40) all have a non-empty first column.
    assert day_marker_indexes == [0, 1, 2, 4, 6, 14, 23, 32, 40]


def test_extract_raw_rows_time_label_forward_fill_source_is_none_between_rows():
    rows, _ = extract_raw_rows(FIXTURE_PATH)
    assert rows[2].time_label == "8.30-10.00"
    assert rows[3].time_label == "10.10-11.40"
    # row 11 is a continuation row (no time label of its own)
    assert rows[11].time_label is None


def test_find_group_column_index_locates_target_group():
    rows, _ = extract_raw_rows(FIXTURE_PATH)
    header_idx, col_idx = find_group_column_index(rows, "Б-Мв-101")
    assert header_idx == 1
    assert col_idx == 4


def test_find_group_column_index_raises_for_unknown_group():
    rows, _ = extract_raw_rows(FIXTURE_PATH)
    with pytest.raises(ValueError):
        find_group_column_index(rows, "NO-SUCH-GROUP")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_parser_extraction.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.pdf_parser'`

- [ ] **Step 3: Implement the extraction portion of `schedule_bot/pdf_parser.py`**

```python
import io
from dataclasses import dataclass

import pdfplumber


@dataclass
class Cell:
    text: str | None
    x0: float
    x1: float


@dataclass
class RawRow:
    day_marker: bool
    time_label: str | None
    y0: float
    y1: float
    cells: list["Cell | None"]


def _load_pdf_bytes(source: str) -> bytes:
    if source.startswith("http://") or source.startswith("https://"):
        import requests

        response = requests.get(source, timeout=30)
        response.raise_for_status()
        return response.content
    with open(source, "rb") as handle:
        return handle.read()


def extract_raw_rows(source: str) -> tuple[list[RawRow], tuple[float, float, float, float]]:
    pdf_bytes = _load_pdf_bytes(source)
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        page = pdf.pages[0]
        table = page.find_tables()[0]
        texts = table.extract()
        rows: list[RawRow] = []
        for table_row, row_texts in zip(table.rows, texts):
            cells: list[Cell | None] = []
            for cell_bbox, text in zip(table_row.cells, row_texts):
                if cell_bbox is None:
                    cells.append(None)
                else:
                    cells.append(Cell(text=text, x0=cell_bbox[0], x1=cell_bbox[2]))
            time_label = cells[1].text if len(cells) > 1 and cells[1] is not None else None
            rows.append(
                RawRow(
                    day_marker=cells[0] is not None,
                    time_label=time_label or None,
                    y0=table_row.bbox[1],
                    y1=table_row.bbox[3],
                    cells=cells,
                )
            )
        return rows, table.bbox


def find_group_column_index(rows: list[RawRow], group_name: str) -> tuple[int, int]:
    for row_index, row in enumerate(rows):
        for col_index, cell in enumerate(row.cells):
            if cell is not None and cell.text and group_name in cell.text:
                return row_index, col_index
    raise ValueError(f"Column for group {group_name!r} not found in table header")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pdf_parser_extraction.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/pdf_parser.py tests/test_pdf_parser_extraction.py
git commit -m "feat: extract raw table rows and locate group column from schedule PDF"
```

---

## Task 5: Pure schedule-building logic (upper/lower weeks, merged cells)

**Files:**
- Modify: `schedule_bot/pdf_parser.py`
- Test: `tests/test_pdf_parser_build_schedule.py`

**Interfaces:**
- Consumes: `Cell`, `RawRow` from Task 4
- Produces:
  - `schedule_bot.pdf_parser.DAY_ORDER = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]`
  - `schedule_bot.pdf_parser.Lesson` (dataclass: `time: str, upper: str | None, lower: str | None`)
  - `schedule_bot.pdf_parser.DaySchedule` (dataclass: `y0: float, y1: float, lessons: list[Lesson]`)
  - `schedule_bot.pdf_parser.build_schedule(rows: list[RawRow], group_column_index: int, table_x1: float) -> dict[str, DaySchedule]`

This is a pure function — no PDF I/O — so every case below is a synthetic `RawRow` list, not a real PDF. Each test constructs the minimum rows needed to exercise one behavior from the spec.

- [ ] **Step 1: Write the failing tests, one per documented edge case**

```python
# tests/test_pdf_parser_build_schedule.py
from schedule_bot.pdf_parser import Cell, RawRow, Lesson, build_schedule

GROUP_COL = 2  # small synthetic tables use 3 columns: day, time, target group
TABLE_X1 = 500.0


def day_row(time_label, cells, day_marker=False, y0=0.0, y1=10.0):
    return RawRow(day_marker=day_marker, time_label=time_label, y0=y0, y1=y1, cells=cells)


def test_single_unsplit_lesson_applies_to_both_weeks():
    rows = [
        day_row(None, [Cell("Пн", 0, 30), None, None], day_marker=True, y0=0, y1=10),
        day_row("8.30-10.00", [None, Cell("8.30-10.00", 30, 60), Cell("Матан", 300, 500)], y0=0, y1=10),
    ]
    schedule = build_schedule(rows, GROUP_COL, TABLE_X1)
    lessons = schedule["Понедельник"].lessons
    assert lessons == [Lesson(time="8.30-10.00", upper="Матан", lower="Матан")]


def test_split_slot_upper_is_first_row_lower_is_second():
    rows = [
        day_row(None, [Cell("Пн", 0, 30), None, None], day_marker=True, y0=0, y1=20),
        day_row("15.20-16.50", [None, Cell("15.20-16.50", 30, 60), Cell("Лекция", 300, 500)], y0=0, y1=10),
        day_row(None, [None, None, Cell("Семинар", 300, 500)], y0=10, y1=20),
    ]
    schedule = build_schedule(rows, GROUP_COL, TABLE_X1)
    lessons = schedule["Понедельник"].lessons
    assert lessons[0].upper == "Лекция"
    assert lessons[0].lower == "Семинар"


def test_empty_upper_with_content_in_lower_preserves_position():
    rows = [
        day_row(None, [Cell("Чт", 0, 30), None, None], day_marker=True, y0=0, y1=20),
        day_row("10.10-11.40", [None, Cell("10.10-11.40", 30, 60), Cell("", 300, 500)], y0=0, y1=10),
        day_row(None, [None, None, Cell("Математика", 300, 500)], y0=10, y1=20),
    ]
    schedule = build_schedule(rows, GROUP_COL, TABLE_X1)
    lesson = schedule["Четверг"].lessons[0]
    assert lesson.upper is None
    assert lesson.lower == "Математика"


def test_merged_cell_spanning_to_table_edge_applies_to_target_group():
    # Target group's own cell is missing (None) for this row, but an earlier
    # column's cell reaches the table's right edge -> shared/merged lesson.
    rows = [
        day_row(None, [Cell("Сб", 0, 30), None, None], day_marker=True, y0=0, y1=20),
        day_row("12.00-13.30", [None, Cell("12.00-13.30", 30, 60), Cell("", 300, 500)], y0=0, y1=10),
        day_row(None, [None, None, Cell("Общая пара для всех групп", 60, 500), None], y0=10, y1=20),
    ]
    schedule = build_schedule(rows, GROUP_COL, TABLE_X1)
    lesson = schedule["Суббота"].lessons[0]
    assert lesson.upper is None
    assert lesson.lower == "Общая пара для всех групп"


def test_non_merged_empty_continuation_row_is_ignored_not_recorded():
    # The earlier column's cell in the continuation row does NOT reach the
    # table's right edge, and the target group has no cell at all here ->
    # this row is irrelevant to the target group; the slot stays unsplit.
    rows = [
        day_row(None, [Cell("Сб", 0, 30), None, None], day_marker=True, y0=0, y1=20),
        day_row("8.30-10.00", [None, Cell("8.30-10.00", 30, 60), Cell("Основы права", 300, 500)], y0=0, y1=10),
        day_row(None, [None, None, Cell("Другая группа занята", 60, 250), None], y0=10, y1=20),
    ]
    schedule = build_schedule(rows, GROUP_COL, TABLE_X1)
    lessons = schedule["Суббота"].lessons
    assert len(lessons) == 1
    assert lessons[0].upper == "Основы права"
    assert lessons[0].lower == "Основы права"


def test_slot_empty_in_both_weeks_is_omitted():
    rows = [
        day_row(None, [Cell("Пн", 0, 30), None, None], day_marker=True, y0=0, y1=20),
        day_row("8.30-10.00", [None, Cell("8.30-10.00", 30, 60), Cell("", 300, 500)], y0=0, y1=10),
        day_row(None, [None, None, None], y0=10, y1=20),
    ]
    schedule = build_schedule(rows, GROUP_COL, TABLE_X1)
    assert schedule["Понедельник"].lessons == []


def test_day_bbox_spans_min_y0_to_max_y1_across_all_rows():
    rows = [
        day_row(None, [Cell("Пн", 0, 30), None, None], day_marker=True, y0=5.0, y1=15.0),
        day_row("8.30-10.00", [None, Cell("8.30-10.00", 30, 60), Cell("Матан", 300, 500)], y0=5.0, y1=15.0),
        day_row("10.10-11.40", [None, Cell("10.10-11.40", 30, 60), Cell("Физика", 300, 500)], y0=15.0, y1=25.0),
    ]
    schedule = build_schedule(rows, GROUP_COL, TABLE_X1)
    assert schedule["Понедельник"].y0 == 5.0
    assert schedule["Понедельник"].y1 == 25.0


def test_stops_after_sixth_day_marker_ignoring_footer_row():
    rows = []
    for day_index in range(6):
        rows.append(
            day_row(None, [Cell(f"day{day_index}", 0, 30), None, None], day_marker=True, y0=0, y1=10)
        )
        rows.append(
            day_row("8.30-10.00", [None, Cell("8.30-10.00", 30, 60), Cell("Урок", 300, 500)], y0=0, y1=10)
        )
    # a 7th "day marker" row, e.g. a footer signature line
    rows.append(day_row(None, [Cell("Начальник отдела", 0, 30), None, None], day_marker=True, y0=0, y1=10))
    schedule = build_schedule(rows, GROUP_COL, TABLE_X1)
    assert set(schedule.keys()) == {
        "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_parser_build_schedule.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_schedule'`

- [ ] **Step 3: Implement `build_schedule` and its supporting types in `schedule_bot/pdf_parser.py`**

```python
# append to schedule_bot/pdf_parser.py

DAY_ORDER = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]


@dataclass
class Lesson:
    time: str
    upper: str | None
    lower: str | None


@dataclass
class DaySchedule:
    y0: float
    y1: float
    lessons: list[Lesson]


class _DayAccumulator:
    def __init__(self) -> None:
        self.y0: float | None = None
        self.y1: float | None = None
        self._slots: dict[str, list[str | None]] = {}
        self._order: list[str] = []

    def add_row(self, time_label: str, text: str | None, y0: float, y1: float) -> None:
        self.y0 = y0 if self.y0 is None else min(self.y0, y0)
        self.y1 = y1 if self.y1 is None else max(self.y1, y1)
        if time_label not in self._slots:
            self._slots[time_label] = []
            self._order.append(time_label)
        self._slots[time_label].append(text)

    def finalize(self) -> DaySchedule:
        lessons = []
        for time_label in self._order:
            values = self._slots[time_label]
            if all(value is None for value in values):
                continue
            upper = values[0]
            lower = values[1] if len(values) > 1 else values[0]
            lessons.append(Lesson(time=time_label, upper=upper, lower=lower))
        return DaySchedule(y0=self.y0 or 0.0, y1=self.y1 or 0.0, lessons=lessons)


def build_schedule(
    rows: list[RawRow], group_column_index: int, table_x1: float, epsilon: float = 2.0
) -> dict[str, DaySchedule]:
    day_names = iter(DAY_ORDER)
    current_day: str | None = None
    current_time: str | None = None
    accumulators: dict[str, _DayAccumulator] = {}

    for row in rows:
        if row.day_marker:
            try:
                current_day = next(day_names)
            except StopIteration:
                break
            accumulators[current_day] = _DayAccumulator()

        if current_day is None:
            continue

        if row.time_label is not None:
            current_time = row.time_label
        if current_time is None:
            continue

        cell = row.cells[group_column_index] if group_column_index < len(row.cells) else None
        if cell is not None:
            text = cell.text or None
        else:
            text = None
            for other in row.cells[:group_column_index]:
                if other is not None and other.text and other.x1 >= table_x1 - epsilon:
                    text = other.text
                    break
            else:
                continue
            if text is None:
                continue

        accumulators[current_day].add_row(current_time, text, row.y0, row.y1)

    return {day: acc.finalize() for day, acc in accumulators.items()}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pdf_parser_build_schedule.py -v`
Expected: PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/pdf_parser.py tests/test_pdf_parser_build_schedule.py
git commit -m "feat: build day/time schedule structure with week-split and merged-cell handling"
```

---

## Task 6: Top-level `parse_schedule`, `Schedule` type, and JSON (de)serialization

**Files:**
- Modify: `schedule_bot/pdf_parser.py`
- Test: `tests/test_pdf_parser_integration.py`

**Interfaces:**
- Consumes: everything from Tasks 4-5
- Produces:
  - `schedule_bot.pdf_parser.Schedule` (dataclass: `table_x0: float, table_x1: float, days: dict[str, DaySchedule]`)
  - `schedule_bot.pdf_parser.parse_schedule(source: str, group_name: str) -> Schedule`
  - `schedule_bot.pdf_parser.schedule_to_dict(schedule: Schedule) -> dict`
  - `schedule_bot.pdf_parser.schedule_from_dict(data: dict) -> Schedule`

- [ ] **Step 1: Write the failing integration test against the real fixture**

These expected values were derived by hand-tracing every row of `tests/fixtures/schedule_sample.pdf` against the `build_schedule` algorithm (see Task 5) — this is a full regression check, not a guess.

```python
# tests/test_pdf_parser_integration.py
from pathlib import Path
import json
import pytest
from schedule_bot.pdf_parser import parse_schedule, schedule_to_dict, schedule_from_dict

FIXTURE_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


@pytest.fixture(scope="module")
def schedule():
    return parse_schedule(FIXTURE_PATH, "Б-Мв-101")


def test_monday_and_tuesday_have_no_lessons(schedule):
    assert schedule.days["Понедельник"].lessons == []
    assert schedule.days["Вторник"].lessons == []


def test_wednesday_lessons(schedule):
    lessons = {l.time: (l.upper, l.lower) for l in schedule.days["Среда"].lessons}
    assert set(lessons) == {"13.40-15.10", "15.20-16.50", "17.00-18.30"}
    upper, lower = lessons["15.20-16.50"]
    assert "СПЕЦ. ПЕДАГОГИКА" in upper
    assert "Специальная педагогика" in lower


def test_thursday_has_asymmetric_split_at_10_10(schedule):
    lessons = {l.time: (l.upper, l.lower) for l in schedule.days["Четверг"].lessons}
    upper, lower = lessons["10.10-11.40"]
    assert upper is None
    assert "МАТЕМАТИКА" in lower


def test_saturday_has_merged_cell_at_12_00(schedule):
    lessons = {l.time: (l.upper, l.lower) for l in schedule.days["Суббота"].lessons}
    upper, lower = lessons["12.00-13.30"]
    assert upper is None
    assert "ОСНОВЫ РОССИЙСКОЙ ГОСУДАРСТВЕННОСТИ" in lower


def test_saturday_8_30_slot_is_fully_empty_and_omitted(schedule):
    lessons = {l.time for l in schedule.days["Суббота"].lessons}
    assert "8.30-10.00" not in lessons


def test_parse_schedule_raises_for_unknown_group():
    with pytest.raises(ValueError):
        parse_schedule(FIXTURE_PATH, "NO-SUCH-GROUP")


def test_schedule_round_trips_through_json(schedule):
    payload = json.dumps(schedule_to_dict(schedule))
    restored = schedule_from_dict(json.loads(payload))
    assert restored.table_x0 == schedule.table_x0
    assert restored.table_x1 == schedule.table_x1
    assert restored.days["Среда"].lessons == schedule.days["Среда"].lessons
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_pdf_parser_integration.py -v`
Expected: FAIL with `ImportError: cannot import name 'parse_schedule'`

- [ ] **Step 3: Implement `Schedule`, `parse_schedule`, and (de)serialization**

```python
# append to schedule_bot/pdf_parser.py
from dataclasses import asdict


@dataclass
class Schedule:
    table_x0: float
    table_x1: float
    days: dict[str, DaySchedule]


def parse_schedule(source: str, group_name: str) -> Schedule:
    rows, table_bbox = extract_raw_rows(source)
    header_idx, col_idx = find_group_column_index(rows, group_name)
    body_rows = rows[header_idx + 1 :]
    days = build_schedule(body_rows, col_idx, table_bbox[2])
    return Schedule(table_x0=table_bbox[0], table_x1=table_bbox[2], days=days)


def schedule_to_dict(schedule: Schedule) -> dict:
    return {
        "table_x0": schedule.table_x0,
        "table_x1": schedule.table_x1,
        "days": {
            day: {
                "y0": day_schedule.y0,
                "y1": day_schedule.y1,
                "lessons": [asdict(lesson) for lesson in day_schedule.lessons],
            }
            for day, day_schedule in schedule.days.items()
        },
    }


def schedule_from_dict(data: dict) -> Schedule:
    days = {
        day: DaySchedule(
            y0=value["y0"],
            y1=value["y1"],
            lessons=[Lesson(**lesson) for lesson in value["lessons"]],
        )
        for day, value in data["days"].items()
    }
    return Schedule(table_x0=data["table_x0"], table_x1=data["table_x1"], days=days)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_pdf_parser_integration.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/pdf_parser.py tests/test_pdf_parser_integration.py
git commit -m "feat: add parse_schedule entry point and JSON (de)serialization"
```

---

## Task 7: Diff computation between schedule versions

**Files:**
- Create: `schedule_bot/diff.py`
- Test: `tests/test_diff.py`

**Interfaces:**
- Consumes: `Schedule`, `DaySchedule`, `Lesson`, `DAY_ORDER` from `schedule_bot.pdf_parser`
- Produces: `schedule_bot.diff.compute_diff(old: Schedule | None, new: Schedule) -> list[str]`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_diff.py
from schedule_bot.diff import compute_diff
from schedule_bot.pdf_parser import Schedule, DaySchedule, Lesson


def make_schedule(**days_lessons):
    days = {
        day: DaySchedule(y0=0.0, y1=10.0, lessons=lessons)
        for day, lessons in days_lessons.items()
    }
    return Schedule(table_x0=0.0, table_x1=500.0, days=days)


def test_no_changes_returns_empty_list():
    schedule = make_schedule(
        Понедельник=[Lesson(time="8.30-10.00", upper="Матан", lower="Матан")]
    )
    assert compute_diff(schedule, schedule) == []


def test_first_version_against_none_produces_no_diff_entries():
    schedule = make_schedule(
        Понедельник=[Lesson(time="8.30-10.00", upper="Матан", lower="Матан")]
    )
    assert compute_diff(None, schedule) == []


def test_changed_lesson_is_reported():
    old = make_schedule(
        Понедельник=[Lesson(time="8.30-10.00", upper="Матан", lower="Матан")]
    )
    new = make_schedule(
        Понедельник=[Lesson(time="8.30-10.00", upper="Физика", lower="Матан")]
    )
    changes = compute_diff(old, new)
    assert len(changes) == 1
    assert "Понедельник" in changes[0]
    assert "8.30-10.00" in changes[0]
    assert "Матан" in changes[0]
    assert "Физика" in changes[0]


def test_added_lesson_is_reported():
    old = make_schedule(Понедельник=[])
    new = make_schedule(
        Понедельник=[Lesson(time="8.30-10.00", upper="Физика", lower=None)]
    )
    changes = compute_diff(old, new)
    assert len(changes) == 1
    assert "нет пары" in changes[0]
    assert "Физика" in changes[0]


def test_unaffected_days_produce_no_entries():
    old = make_schedule(
        Понедельник=[Lesson(time="8.30-10.00", upper="Матан", lower="Матан")],
        Вторник=[Lesson(time="8.30-10.00", upper="Химия", lower="Химия")],
    )
    new = make_schedule(
        Понедельник=[Lesson(time="8.30-10.00", upper="Физика", lower="Матан")],
        Вторник=[Lesson(time="8.30-10.00", upper="Химия", lower="Химия")],
    )
    changes = compute_diff(old, new)
    assert len(changes) == 1
    assert "Вторник" not in changes[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_diff.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.diff'`

- [ ] **Step 3: Implement `schedule_bot/diff.py`**

```python
from schedule_bot.pdf_parser import DAY_ORDER, Schedule


def _describe(pair: tuple[str | None, str | None] | None) -> str:
    if pair is None:
        return "нет пары"
    upper, lower = pair
    if upper == lower:
        return upper or "нет пары"
    return f"верхняя: {upper or 'нет пары'}; нижняя: {lower or 'нет пары'}"


def compute_diff(old: Schedule | None, new: Schedule) -> list[str]:
    changes: list[str] = []
    old_days = old.days if old is not None else {}

    for day in DAY_ORDER:
        old_day = old_days.get(day)
        old_lessons = {
            lesson.time: (lesson.upper, lesson.lower) for lesson in (old_day.lessons if old_day else [])
        }
        new_day = new.days.get(day)
        new_lessons = {
            lesson.time: (lesson.upper, lesson.lower) for lesson in (new_day.lessons if new_day else [])
        }
        all_times = sorted(set(old_lessons) | set(new_lessons))
        for time_label in all_times:
            old_pair = old_lessons.get(time_label)
            new_pair = new_lessons.get(time_label)
            if old_pair != new_pair:
                changes.append(
                    f"{day} {time_label}: {_describe(old_pair)} -> {_describe(new_pair)}"
                )
    return changes
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_diff.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/diff.py tests/test_diff.py
git commit -m "feat: compute human-readable diff between two schedule versions"
```

---

## Task 8: Day-screenshot rendering

**Files:**
- Create: `schedule_bot/screenshot.py`
- Test: `tests/test_screenshot.py`

**Interfaces:**
- Consumes: `Schedule` from `schedule_bot.pdf_parser`, `tests/fixtures/schedule_sample.pdf`
- Produces: `schedule_bot.screenshot.render_day_image(pdf_path: str, schedule: Schedule, day: str, zoom: float = 2.5, padding: float = 4.0) -> bytes | None`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_screenshot.py
from pathlib import Path
from schedule_bot.pdf_parser import parse_schedule
from schedule_bot.screenshot import render_day_image

FIXTURE_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


def test_render_day_image_returns_png_bytes_for_a_day_with_lessons():
    schedule = parse_schedule(FIXTURE_PATH, "Б-Мв-101")
    image_bytes = render_day_image(FIXTURE_PATH, schedule, "Среда")
    assert image_bytes is not None
    assert image_bytes[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_day_image_returns_none_for_a_day_with_no_lessons():
    schedule = parse_schedule(FIXTURE_PATH, "Б-Мв-101")
    assert render_day_image(FIXTURE_PATH, schedule, "Понедельник") is None


def test_render_day_image_returns_none_for_unknown_day():
    schedule = parse_schedule(FIXTURE_PATH, "Б-Мв-101")
    assert render_day_image(FIXTURE_PATH, schedule, "NotADay") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_screenshot.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.screenshot'`

- [ ] **Step 3: Implement `schedule_bot/screenshot.py`**

```python
import fitz

from schedule_bot.pdf_parser import Schedule


def render_day_image(
    pdf_path: str, schedule: Schedule, day: str, zoom: float = 2.5, padding: float = 4.0
) -> bytes | None:
    day_schedule = schedule.days.get(day)
    if day_schedule is None or not day_schedule.lessons:
        return None

    document = fitz.open(pdf_path)
    try:
        page = document[0]
        clip = fitz.Rect(
            schedule.table_x0,
            max(day_schedule.y0 - padding, 0.0),
            schedule.table_x1,
            day_schedule.y1 + padding,
        )
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=clip)
        return pixmap.tobytes("png")
    finally:
        document.close()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_screenshot.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/screenshot.py tests/test_screenshot.py
git commit -m "feat: render cropped day-band screenshot from schedule PDF"
```

---

## Task 9: Message formatting

**Files:**
- Create: `schedule_bot/formatting.py`
- Test: `tests/test_formatting.py`

**Interfaces:**
- Consumes: `Schedule`, `DaySchedule`, `Lesson` from `schedule_bot.pdf_parser`; `WEEK_UPPER`, `WEEK_LOWER` from `schedule_bot.weektype`
- Produces: `schedule_bot.formatting.format_day_schedule(day: str, on_date: datetime.date, week_type: str, schedule: Schedule) -> str`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_formatting.py
from datetime import date
from schedule_bot.formatting import format_day_schedule
from schedule_bot.pdf_parser import Schedule, DaySchedule, Lesson
from schedule_bot.weektype import WEEK_UPPER, WEEK_LOWER


def make_schedule():
    return Schedule(
        table_x0=0.0,
        table_x1=500.0,
        days={
            "Среда": DaySchedule(
                y0=0.0,
                y1=10.0,
                lessons=[
                    Lesson(time="8.30-10.00", upper="Лекция", lower="Семинар"),
                    Lesson(time="10.10-11.40", upper=None, lower="Математика"),
                ],
            ),
            "Понедельник": DaySchedule(y0=0.0, y1=10.0, lessons=[]),
        },
    )


def test_format_shows_upper_variant_on_upper_week():
    text = format_day_schedule("Среда", date(2026, 9, 16), WEEK_UPPER, make_schedule())
    assert "Лекция" in text
    assert "Семинар" not in text
    assert "нет пары" in text  # 10.10-11.40 has no upper-week lesson


def test_format_shows_lower_variant_on_lower_week():
    text = format_day_schedule("Среда", date(2026, 9, 16), WEEK_LOWER, make_schedule())
    assert "Семинар" in text
    assert "Математика" in text
    assert "Лекция" not in text


def test_format_reports_no_lessons_for_empty_day():
    text = format_day_schedule("Понедельник", date(2026, 9, 16), WEEK_UPPER, make_schedule())
    assert "Занятий нет" in text


def test_format_reports_no_lessons_for_unknown_day():
    text = format_day_schedule("Воскресенье", date(2026, 9, 16), WEEK_UPPER, make_schedule())
    assert "Занятий нет" in text


def test_format_includes_day_name_date_and_week_type():
    text = format_day_schedule("Среда", date(2026, 9, 16), WEEK_UPPER, make_schedule())
    assert "Среда" in text
    assert "16.09.2026" in text
    assert WEEK_UPPER in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_formatting.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.formatting'`

- [ ] **Step 3: Implement `schedule_bot/formatting.py`**

```python
from datetime import date

from schedule_bot.pdf_parser import Schedule
from schedule_bot.weektype import WEEK_UPPER


def format_day_schedule(day: str, on_date: date, week_type: str, schedule: Schedule) -> str:
    header = f"{day}, {on_date.strftime('%d.%m.%Y')} ({week_type} неделя)"
    day_schedule = schedule.days.get(day)
    if day_schedule is None or not day_schedule.lessons:
        return f"{header}\n\nЗанятий нет."

    lines = [header, ""]
    for lesson in day_schedule.lessons:
        text = lesson.upper if week_type == WEEK_UPPER else lesson.lower
        lines.append(f"{lesson.time} — {text or 'нет пары'}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_formatting.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/formatting.py tests/test_formatting.py
git commit -m "feat: format a day's schedule text for the current week type"
```

---

## Task 10: Sync orchestration (`check_and_apply_diff`)

**Files:**
- Create: `schedule_bot/sync.py`
- Test: `tests/test_sync.py`

**Interfaces:**
- Consumes: `schedule_bot.storage` (Task 2), `schedule_bot.pdf_parser` (Tasks 4-6), `schedule_bot.diff` (Task 7)
- Produces:
  - `schedule_bot.sync.SyncResult` (dataclass: `status: str` — `"no_changes"`, `"applied"`, or `"initial"`; `changes: list[str]`)
  - `async def schedule_bot.sync.check_and_apply_diff(conn, pdf_source: str, group_name: str, pdf_cache_dir: str, notify) -> SyncResult`
    where `notify` is an async callable `notify(chat_id: int, text: str) -> None` (the caller supplies `bot.send_message` or a test double)

- [ ] **Step 1: Write the failing tests**

Use the real fixture PDF as the "downloaded" source (local path, exercising the same `pdf_source` handling used in production), and a fake `notify` to record calls without touching Telegram.

```python
# tests/test_sync.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_sync.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.sync'`

- [ ] **Step 3: Implement `schedule_bot/sync.py`**

```python
import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from schedule_bot import storage
from schedule_bot.diff import compute_diff
from schedule_bot.pdf_parser import parse_schedule, schedule_from_dict, schedule_to_dict

Notifier = Callable[[int, str], Awaitable[None]]


@dataclass
class SyncResult:
    status: str
    changes: list[str] = field(default_factory=list)


def _cache_pdf(pdf_source: str, pdf_cache_dir: str, group_name: str) -> str:
    cache_dir = Path(pdf_cache_dir) / group_name
    cache_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    destination = cache_dir / f"{timestamp}.pdf"
    if pdf_source.startswith("http://") or pdf_source.startswith("https://"):
        import requests

        response = requests.get(pdf_source, timeout=30)
        response.raise_for_status()
        destination.write_bytes(response.content)
    else:
        shutil.copyfile(pdf_source, destination)
    return str(destination)


async def check_and_apply_diff(
    conn, pdf_source: str, group_name: str, pdf_cache_dir: str, notify: Notifier
) -> SyncResult:
    active = storage.get_active_version(conn, group_name)
    old_schedule = schedule_from_dict(json.loads(active.payload_json)) if active else None

    new_schedule = parse_schedule(pdf_source, group_name)
    changes = compute_diff(old_schedule, new_schedule)

    if active is not None and not changes:
        return SyncResult(status="no_changes")

    cache_path = _cache_pdf(pdf_source, pdf_cache_dir, group_name)
    payload_json = json.dumps(schedule_to_dict(new_schedule))
    parsed_at = datetime.now(timezone.utc).isoformat()
    storage.insert_new_version(conn, group_name, payload_json, cache_path, parsed_at)

    if active is None:
        return SyncResult(status="initial")

    message = "Расписание изменилось:\n" + "\n".join(changes)
    for chat_id in storage.list_subscribers(conn, group_name):
        await notify(chat_id, message)

    return SyncResult(status="applied", changes=changes)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_sync.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/sync.py tests/test_sync.py
git commit -m "feat: orchestrate parse-diff-apply-notify sync cycle"
```

---

## Task 11: Telegram bot handlers (user commands)

**Files:**
- Create: `schedule_bot/bot.py` (user-facing portion — admin commands are Task 12)
- Test: `tests/test_bot_user_commands.py`

**Interfaces:**
- Consumes: `schedule_bot.storage`, `schedule_bot.pdf_parser.{Schedule, DAY_ORDER, schedule_from_dict}`, `schedule_bot.formatting.format_day_schedule`, `schedule_bot.screenshot.render_day_image`, `schedule_bot.weektype.WeekFormula`
- Produces:
  - `schedule_bot.bot.DAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]` (index-aligned with `pdf_parser.DAY_ORDER`)
  - `schedule_bot.bot.build_day_keyboard() -> telegram.InlineKeyboardMarkup`
  - `schedule_bot.bot.day_index_from_callback(data: str) -> int`
  - `async def schedule_bot.bot.start_command(update, context) -> None`
  - `async def schedule_bot.bot.stop_command(update, context) -> None`
  - `async def schedule_bot.bot.schedule_command(update, context) -> None`
  - `async def schedule_bot.bot.day_callback(update, context) -> None`

Handlers read shared dependencies off `context.bot_data`: `context.bot_data["conn"]` (sqlite connection), `context.bot_data["week_formula"]` (`WeekFormula`), `context.bot_data["pdf_cache_path_getter"]` — a zero-arg callable returning the active version's cached PDF path for the configured group (kept as a callable so tests can stub it without touching storage).

- [ ] **Step 1: Write the failing tests for the keyboard helpers**

```python
# tests/test_bot_user_commands.py
from datetime import date
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.bot import (
    DAY_LABELS,
    build_day_keyboard,
    day_index_from_callback,
    start_command,
    stop_command,
    schedule_command,
    day_callback,
)
from schedule_bot.pdf_parser import DAY_ORDER, Schedule, DaySchedule, Lesson, schedule_to_dict
from schedule_bot.weektype import WeekFormula, WEEK_UPPER


def test_build_day_keyboard_has_one_button_per_day():
    keyboard = build_day_keyboard()
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert len(buttons) == len(DAY_ORDER) == len(DAY_LABELS)
    assert [button.callback_data for button in buttons] == [f"day:{i}" for i in range(len(DAY_ORDER))]


def test_day_index_from_callback_parses_valid_data():
    assert day_index_from_callback("day:3") == 3


def test_day_index_from_callback_rejects_bad_data():
    with pytest.raises(ValueError):
        day_index_from_callback("nope")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bot_user_commands.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.bot'`

- [ ] **Step 3: Implement the keyboard helpers and `start`/`stop` handlers**

```python
# schedule_bot/bot.py
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from schedule_bot import storage
from schedule_bot.pdf_parser import DAY_ORDER

DAY_LABELS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]


def build_day_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        InlineKeyboardButton(label, callback_data=f"day:{index}")
        for index, label in enumerate(DAY_LABELS)
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    return InlineKeyboardMarkup(rows)


def day_index_from_callback(data: str) -> int:
    if not data.startswith("day:"):
        raise ValueError(f"Unexpected callback data: {data!r}")
    return int(data.split(":", 1)[1])


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    group_name = context.bot_data["group_name"]
    chat_id = update.effective_chat.id
    storage.add_subscriber(conn, chat_id, group_name, datetime.now(timezone.utc).isoformat())
    await update.message.reply_text(
        "Подписка оформлена. Каждое утро вы будете получать расписание. "
        "Команда /schedule покажет расписание на выбранный день."
    )


async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    conn = context.bot_data["conn"]
    chat_id = update.effective_chat.id
    storage.remove_subscriber(conn, chat_id)
    await update.message.reply_text("Подписка отменена.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_bot_user_commands.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Write the failing tests for `/schedule` and the day callback**

```python
# append to tests/test_bot_user_commands.py

def make_context(conn, week_formula, active_schedule, pdf_cache_path="/tmp/does-not-matter.pdf"):
    context = MagicMock()
    context.bot_data = {
        "conn": conn,
        "group_name": "Б-Мв-101",
        "week_formula": week_formula,
        "active_schedule_getter": lambda: active_schedule,
        "active_pdf_path_getter": lambda: pdf_cache_path,
    }
    return context


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


@pytest.fixture
def sample_schedule():
    return Schedule(
        table_x0=0.0,
        table_x1=500.0,
        days={
            "Среда": DaySchedule(
                y0=0.0, y1=10.0, lessons=[Lesson(time="8.30-10.00", upper="Лекция", lower="Семинар")]
            )
        },
    )


@pytest.mark.asyncio
async def test_schedule_command_sends_day_keyboard(conn, sample_schedule):
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    context = make_context(conn, formula, sample_schedule)

    await schedule_command(update, context)

    update.message.reply_text.assert_awaited_once()
    _, kwargs = update.message.reply_text.call_args
    assert "reply_markup" in kwargs


@pytest.mark.asyncio
async def test_day_callback_sends_text_and_image_when_lessons_exist(conn, sample_schedule, monkeypatch):
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    wednesday_index = DAY_ORDER.index("Среда")

    update = MagicMock()
    update.callback_query.data = f"day:{wednesday_index}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()
    context = make_context(conn, formula, sample_schedule)

    import schedule_bot.bot as bot_module
    monkeypatch.setattr(bot_module, "render_day_image", lambda *args, **kwargs: b"fake-png-bytes")

    await day_callback(update, context)

    update.callback_query.answer.assert_awaited_once()
    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_awaited_once()


@pytest.mark.asyncio
async def test_day_callback_skips_image_when_no_lessons(conn, sample_schedule, monkeypatch):
    formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=True)
    monday_index = DAY_ORDER.index("Понедельник")  # not present in sample_schedule.days

    update = MagicMock()
    update.callback_query.data = f"day:{monday_index}"
    update.callback_query.answer = AsyncMock()
    update.callback_query.message.reply_text = AsyncMock()
    update.callback_query.message.reply_photo = AsyncMock()
    context = make_context(conn, formula, sample_schedule)

    import schedule_bot.bot as bot_module
    monkeypatch.setattr(bot_module, "render_day_image", lambda *args, **kwargs: None)

    await day_callback(update, context)

    update.callback_query.message.reply_text.assert_awaited_once()
    update.callback_query.message.reply_photo.assert_not_awaited()
```

Add `pytest-asyncio` markers by creating `pytest.ini`:

```ini
[pytest]
asyncio_mode = auto
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest tests/test_bot_user_commands.py -v`
Expected: FAIL with `ImportError: cannot import name 'schedule_command'`

- [ ] **Step 7: Implement `schedule_command` and `day_callback`**

```python
# append to schedule_bot/bot.py
from datetime import date

from schedule_bot.formatting import format_day_schedule
from schedule_bot.screenshot import render_day_image


async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Выберите день недели:", reply_markup=build_day_keyboard()
    )


async def day_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    day_index = day_index_from_callback(query.data)
    day_name = DAY_ORDER[day_index]

    schedule = context.bot_data["active_schedule_getter"]()
    week_formula: object = context.bot_data["week_formula"]
    week_type = week_formula.compute(date.today())

    text = format_day_schedule(day_name, date.today(), week_type, schedule)
    await query.message.reply_text(text)

    pdf_path = context.bot_data["active_pdf_path_getter"]()
    image_bytes = render_day_image(pdf_path, schedule, day_name)
    if image_bytes is not None:
        await query.message.reply_photo(photo=image_bytes)
```

- [ ] **Step 8: Run test to verify it passes**

Run: `pytest tests/test_bot_user_commands.py -v`
Expected: PASS (7 tests)

- [ ] **Step 9: Commit**

```bash
git add schedule_bot/bot.py tests/test_bot_user_commands.py pytest.ini
git commit -m "feat: add /start, /stop, /schedule, and day-picker callback handlers"
```

---

## Task 12: Telegram bot handlers (admin commands)

**Files:**
- Modify: `schedule_bot/bot.py`
- Test: `tests/test_bot_admin_commands.py`

**Interfaces:**
- Consumes: `schedule_bot.storage`, `schedule_bot.sync.check_and_apply_diff`
- Produces:
  - `async def schedule_bot.bot.set_pdf_source_command(update, context) -> None`
  - `async def schedule_bot.bot.set_group_command(update, context) -> None`
  - `async def schedule_bot.bot.set_send_time_command(update, context) -> None`
  - `async def schedule_bot.bot.set_check_interval_command(update, context) -> None`
  - `async def schedule_bot.bot.refresh_command(update, context) -> None`
  - `async def schedule_bot.bot.alert_command(update, context) -> None`
  - `schedule_bot.bot.is_admin(update, context) -> bool`

Admin handlers read `context.bot_data["admin_chat_id"]` and reuse `context.bot_data["conn"]`. `refresh_command` and the reschedule-triggering setters additionally use `context.bot_data["reschedule_daily_job"]` / `context.bot_data["reschedule_hourly_job"]` (zero-arg callables wired up in Task 13) so this task doesn't need APScheduler itself.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_bot_admin_commands.py
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.bot import (
    is_admin,
    set_pdf_source_command,
    set_group_command,
    set_send_time_command,
    set_check_interval_command,
    refresh_command,
    alert_command,
)

ADMIN_CHAT_ID = 999


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


def make_context(conn, **extra):
    context = MagicMock()
    context.bot_data = {
        "conn": conn,
        "admin_chat_id": ADMIN_CHAT_ID,
        "group_name": "Б-Мв-101",
        "reschedule_daily_job": MagicMock(),
        "reschedule_hourly_job": MagicMock(),
        **extra,
    }
    return context


def make_update(chat_id, args_text=""):
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = f"/cmd {args_text}".strip()
    update.message.reply_text = AsyncMock()
    return update


def test_is_admin_true_for_configured_chat_id(conn):
    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn)
    assert is_admin(update, context) is True


def test_is_admin_false_for_other_chat_id(conn):
    update = make_update(12345)
    context = make_context(conn)
    assert is_admin(update, context) is False


@pytest.mark.asyncio
async def test_set_pdf_source_rejects_non_admin(conn):
    update = make_update(12345, "https://example.com/new.pdf")
    context = make_context(conn)
    context.args = ["https://example.com/new.pdf"]

    await set_pdf_source_command(update, context)

    update.message.reply_text.assert_awaited_once()
    assert storage.get_setting(conn, "pdf_source") is None


@pytest.mark.asyncio
async def test_set_pdf_source_updates_setting_for_admin(conn):
    update = make_update(ADMIN_CHAT_ID, "https://example.com/new.pdf")
    context = make_context(conn)
    context.args = ["https://example.com/new.pdf"]

    await set_pdf_source_command(update, context)

    assert storage.get_setting(conn, "pdf_source") == "https://example.com/new.pdf"


@pytest.mark.asyncio
async def test_set_send_time_reschedules_daily_job(conn):
    update = make_update(ADMIN_CHAT_ID, "07:30")
    context = make_context(conn)
    context.args = ["07:30"]

    await set_send_time_command(update, context)

    assert storage.get_setting(conn, "daily_send_time") == "07:30"
    context.bot_data["reschedule_daily_job"].assert_called_once()


@pytest.mark.asyncio
async def test_set_check_interval_reschedules_hourly_job(conn):
    update = make_update(ADMIN_CHAT_ID, "30")
    context = make_context(conn)
    context.args = ["30"]

    await set_check_interval_command(update, context)

    assert storage.get_setting(conn, "check_interval_minutes") == "30"
    context.bot_data["reschedule_hourly_job"].assert_called_once()


@pytest.mark.asyncio
async def test_refresh_command_reports_result(conn):
    async def fake_sync(*args, **kwargs):
        from schedule_bot.sync import SyncResult

        return SyncResult(status="no_changes")

    update = make_update(ADMIN_CHAT_ID)
    context = make_context(conn, sync_fn=fake_sync)

    await refresh_command(update, context)

    update.message.reply_text.assert_awaited_once()
    assert "Изменений нет" in update.message.reply_text.call_args[0][0]


@pytest.mark.asyncio
async def test_alert_command_broadcasts_to_all_subscribers(conn):
    storage.add_subscriber(conn, 111, "Б-Мв-101", "2026-09-16T00:00:00")
    storage.add_subscriber(conn, 222, "Б-Мв-101", "2026-09-16T00:00:00")
    update = make_update(ADMIN_CHAT_ID, "Внимание, ошибка в расписании")
    context = make_context(conn)
    context.args = ["Внимание,", "ошибка", "в", "расписании"]
    context.bot.send_message = AsyncMock()

    await alert_command(update, context)

    assert context.bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_alert_command_rejects_non_admin(conn):
    update = make_update(12345, "test")
    context = make_context(conn)
    context.args = ["test"]
    context.bot.send_message = AsyncMock()

    await alert_command(update, context)

    context.bot.send_message.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bot_admin_commands.py -v`
Expected: FAIL with `ImportError: cannot import name 'is_admin'`

- [ ] **Step 3: Implement admin handlers**

```python
# append to schedule_bot/bot.py
import sys


def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    return update.effective_chat.id == context.bot_data["admin_chat_id"]


async def set_pdf_source_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /set_pdf_source <url_или_путь>")
        return
    storage.set_setting(context.bot_data["conn"], "pdf_source", context.args[0])
    await update.message.reply_text(f"Источник PDF обновлён: {context.args[0]}")


async def set_group_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /set_group <название>")
        return
    storage.set_setting(context.bot_data["conn"], "group_name", context.args[0])
    await update.message.reply_text(f"Группа обновлена: {context.args[0]}")


async def set_send_time_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /set_send_time <HH:MM>")
        return
    storage.set_setting(context.bot_data["conn"], "daily_send_time", context.args[0])
    context.bot_data["reschedule_daily_job"]()
    await update.message.reply_text(f"Время ежедневной рассылки обновлено: {context.args[0]}")


async def set_check_interval_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /set_check_interval <минуты>")
        return
    storage.set_setting(context.bot_data["conn"], "check_interval_minutes", context.args[0])
    context.bot_data["reschedule_hourly_job"]()
    await update.message.reply_text(f"Интервал проверки обновлён: {context.args[0]} мин.")


async def refresh_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    sync_fn = context.bot_data["sync_fn"]
    result = await sync_fn()
    if result.status == "no_changes":
        await update.message.reply_text("Изменений нет.")
    elif result.status == "initial":
        await update.message.reply_text("Расписание загружено впервые.")
    else:
        await update.message.reply_text(
            "Расписание обновлено и разослано:\n" + "\n".join(result.changes)
        )


async def alert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update, context):
        await update.message.reply_text("Недостаточно прав.")
        return
    if not context.args:
        await update.message.reply_text("Использование: /alert <текст>")
        return
    text = " ".join(context.args)
    conn = context.bot_data["conn"]
    group_name = context.bot_data["group_name"]
    subscribers = storage.list_subscribers(conn, group_name)
    for chat_id in subscribers:
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception as error:
            print(f"bot: failed to alert chat_id={chat_id}: {error}", file=sys.stderr)
    await update.message.reply_text(f"Сообщение разослано {len(subscribers)} подписчикам.")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_bot_admin_commands.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/bot.py tests/test_bot_admin_commands.py
git commit -m "feat: add admin commands for settings, manual refresh, and alerts"
```

---

## Task 13: Scheduler wiring

**Files:**
- Create: `schedule_bot/scheduler.py`
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `schedule_bot.storage`, `schedule_bot.sync.check_and_apply_diff`, `schedule_bot.weektype.fetch_week_formula`
- Produces:
  - `schedule_bot.scheduler.build_scheduler(conn, config, bot, week_formula_holder) -> apscheduler.schedulers.asyncio.AsyncIOScheduler`
  - `schedule_bot.scheduler.reschedule_daily_job(scheduler, conn, timezone_name: str) -> None`
  - `schedule_bot.scheduler.reschedule_hourly_job(scheduler, conn) -> None`

`week_formula_holder` is a small mutable container (`schedule_bot.scheduler.WeekFormulaHolder`, a dataclass with a single `formula: WeekFormula` field) so the weekly refresh job can update it in place and every other component (bot handlers, daily job) always reads the current value through the same reference.

- [ ] **Step 1: Write the failing tests**

Use APScheduler's own introspection (`scheduler.get_job`) rather than waiting for real fire times — the tests only check that jobs are registered with the right trigger configuration and that rescheduling replaces the existing job rather than duplicating it.

```python
# tests/test_scheduler.py
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.scheduler import (
    WeekFormulaHolder,
    build_scheduler,
    reschedule_daily_job,
    reschedule_hourly_job,
)
from schedule_bot.weektype import WeekFormula


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    storage.ensure_default_settings(
        connection,
        {
            "daily_send_time": "09:00",
            "check_interval_minutes": "60",
            "timezone": "Europe/Moscow",
            "pdf_source": "/tmp/does-not-matter.pdf",
            "group_name": "Б-Мв-101",
            "weektype_url": "https://example.com/week",
        },
    )
    return connection


def test_build_scheduler_registers_daily_hourly_and_weekly_jobs(conn):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))

    scheduler = build_scheduler(conn, MagicMock(pdf_cache_dir="/tmp/cache"), bot, holder)

    job_ids = {job.id for job in scheduler.get_jobs()}
    assert job_ids == {"daily_send", "hourly_check", "weekly_weektype_refresh"}


def test_reschedule_daily_job_updates_trigger_time(conn):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = build_scheduler(conn, MagicMock(pdf_cache_dir="/tmp/cache"), bot, holder)

    storage.set_setting(conn, "daily_send_time", "07:15")
    reschedule_daily_job(scheduler, conn, "Europe/Moscow")

    job = scheduler.get_job("daily_send")
    assert job.trigger.fields[  # cron trigger fields: hour, minute
        [f.name for f in job.trigger.fields].index("hour")
    ].expressions[0].first == 7
    assert job.trigger.fields[
        [f.name for f in job.trigger.fields].index("minute")
    ].expressions[0].first == 15


def test_reschedule_hourly_job_updates_interval(conn):
    bot = MagicMock()
    bot.send_message = AsyncMock()
    holder = WeekFormulaHolder(formula=WeekFormula(base_month=1, base_day=5, even_index_is_upper=True))
    scheduler = build_scheduler(conn, MagicMock(pdf_cache_dir="/tmp/cache"), bot, holder)

    storage.set_setting(conn, "check_interval_minutes", "15")
    reschedule_hourly_job(scheduler, conn)

    job = scheduler.get_job("hourly_check")
    assert job.trigger.interval.total_seconds() == 15 * 60
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'schedule_bot.scheduler'`

- [ ] **Step 3: Implement `schedule_bot/scheduler.py`**

```python
import asyncio
import json
from dataclasses import dataclass
from datetime import date

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from schedule_bot import storage
from schedule_bot.formatting import format_day_schedule
from schedule_bot.pdf_parser import DAY_ORDER, schedule_from_dict
from schedule_bot.sync import check_and_apply_diff
from schedule_bot.weektype import WeekFormula, fetch_week_formula


@dataclass
class WeekFormulaHolder:
    formula: WeekFormula


async def _notify(bot, chat_id: int, text: str) -> None:
    await bot.send_message(chat_id=chat_id, text=text)


async def _run_sync(conn, config, bot) -> "SyncResult":
    pdf_source = storage.get_setting(conn, "pdf_source") or config.default_pdf_source
    group_name = storage.get_setting(conn, "group_name") or config.default_group_name
    return await check_and_apply_diff(
        conn, pdf_source, group_name, config.pdf_cache_dir, lambda chat_id, text: _notify(bot, chat_id, text)
    )


async def hourly_check_job(conn, config, bot) -> None:
    await _run_sync(conn, config, bot)


async def daily_send_job(conn, config, bot, week_formula_holder: WeekFormulaHolder) -> None:
    await _run_sync(conn, config, bot)

    group_name = storage.get_setting(conn, "group_name") or config.default_group_name
    active = storage.get_active_version(conn, group_name)
    if active is None:
        return
    schedule = schedule_from_dict(json.loads(active.payload_json))

    today = date.today()
    day_name = DAY_ORDER[today.weekday()] if today.weekday() < len(DAY_ORDER) else None
    if day_name is None:
        return  # Sunday: no classes in this schedule

    week_type = week_formula_holder.formula.compute(today)
    text = format_day_schedule(day_name, today, week_type, schedule)

    for chat_id in storage.list_subscribers(conn, group_name):
        await bot.send_message(chat_id=chat_id, text=text)


async def weekly_weektype_refresh_job(conn, week_formula_holder: WeekFormulaHolder) -> None:
    url = storage.get_setting(conn, "weektype_url")
    if not url:
        return
    try:
        week_formula_holder.formula = fetch_week_formula(url)
    except Exception:
        pass  # keep using the last known-good formula; caller may log this


def _parse_hh_mm(value: str) -> tuple[int, int]:
    hour_str, minute_str = value.split(":", 1)
    return int(hour_str), int(minute_str)


def build_scheduler(conn, config, bot, week_formula_holder: WeekFormulaHolder) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone=storage.get_setting(conn, "timezone") or config.default_timezone)

    send_time = storage.get_setting(conn, "daily_send_time") or config.default_daily_send_time
    hour, minute = _parse_hh_mm(send_time)
    scheduler.add_job(
        daily_send_job,
        CronTrigger(hour=hour, minute=minute),
        args=[conn, config, bot, week_formula_holder],
        id="daily_send",
        replace_existing=True,
    )

    interval_minutes = int(storage.get_setting(conn, "check_interval_minutes") or config.default_check_interval_minutes)
    scheduler.add_job(
        hourly_check_job,
        IntervalTrigger(minutes=interval_minutes),
        args=[conn, config, bot],
        id="hourly_check",
        replace_existing=True,
    )

    scheduler.add_job(
        weekly_weektype_refresh_job,
        CronTrigger(day_of_week="mon", hour=6, minute=0),
        args=[conn, week_formula_holder],
        id="weekly_weektype_refresh",
        replace_existing=True,
    )

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    scheduler.start()
    return scheduler


def reschedule_daily_job(scheduler: AsyncIOScheduler, conn, timezone_name: str) -> None:
    send_time = storage.get_setting(conn, "daily_send_time")
    hour, minute = _parse_hh_mm(send_time)
    scheduler.reschedule_job("daily_send", trigger=CronTrigger(hour=hour, minute=minute, timezone=timezone_name))


def reschedule_hourly_job(scheduler: AsyncIOScheduler, conn) -> None:
    interval_minutes = int(storage.get_setting(conn, "check_interval_minutes"))
    scheduler.reschedule_job("hourly_check", trigger=IntervalTrigger(minutes=interval_minutes))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_scheduler.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/scheduler.py tests/test_scheduler.py
git commit -m "feat: wire APScheduler jobs for daily send, hourly check, weekly week-type refresh"
```

---

## Task 14: Application entrypoint

**Files:**
- Create: `main.py`
- Test: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: everything from Tasks 1-13
- Produces: `main.build_application(config: Config, conn) -> telegram.ext.Application`

`main.py`'s `if __name__ == "__main__":` block does real I/O (reads `os.environ`, opens the real sqlite file, calls `fetch_week_formula` over the network, starts polling) and is intentionally not unit-tested — it is covered by the manual smoke test in Task 15. `build_application` itself takes an already-loaded `Config` and an already-open connection so it can be tested without any real network or filesystem access beyond an in-memory database.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_main_wiring.py
import sqlite3
from unittest.mock import MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.config import Config
from schedule_bot.weektype import WeekFormula
from main import build_application


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


@pytest.fixture
def config():
    return Config(
        telegram_token="test-token",
        admin_chat_id=999,
        db_path=":memory:",
        pdf_cache_dir="/tmp/cache",
        default_pdf_source="/tmp/does-not-matter.pdf",
        default_group_name="Б-Мв-101",
        default_weektype_url="https://example.com/week",
        default_daily_send_time="09:00",
        default_check_interval_minutes=60,
        default_timezone="Europe/Moscow",
    )


def test_build_application_registers_all_expected_handlers(conn, config, monkeypatch):
    monkeypatch.setattr(
        "main.fetch_week_formula",
        lambda url: WeekFormula(base_month=1, base_day=5, even_index_is_upper=True),
    )

    application = build_application(config, conn)

    command_names = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "commands"):
                command_names.update(handler.commands)

    assert {
        "start",
        "stop",
        "schedule",
        "set_pdf_source",
        "set_group",
        "set_send_time",
        "set_check_interval",
        "refresh",
        "alert",
    }.issubset(command_names)


def test_build_application_seeds_default_settings_from_config(conn, config, monkeypatch):
    monkeypatch.setattr(
        "main.fetch_week_formula",
        lambda url: WeekFormula(base_month=1, base_day=5, even_index_is_upper=True),
    )

    build_application(config, conn)

    assert storage.get_setting(conn, "pdf_source") == "/tmp/does-not-matter.pdf"
    assert storage.get_setting(conn, "group_name") == "Б-Мв-101"
    assert storage.get_setting(conn, "daily_send_time") == "09:00"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_main_wiring.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'main'`

- [ ] **Step 3: Implement `main.py`**

```python
import os

from telegram.ext import Application, CallbackQueryHandler, CommandHandler

from schedule_bot import storage
from schedule_bot.bot import (
    alert_command,
    day_callback,
    refresh_command,
    schedule_command,
    set_check_interval_command,
    set_group_command,
    set_pdf_source_command,
    set_send_time_command,
    start_command,
    stop_command,
)
from schedule_bot.config import Config, load_config
from schedule_bot.scheduler import (
    WeekFormulaHolder,
    build_scheduler,
    reschedule_daily_job,
    reschedule_hourly_job,
)
from schedule_bot.sync import check_and_apply_diff
from schedule_bot.weektype import fetch_week_formula


def build_application(config: Config, conn) -> Application:
    storage.ensure_default_settings(
        conn,
        {
            "pdf_source": config.default_pdf_source,
            "group_name": config.default_group_name,
            "weektype_url": config.default_weektype_url,
            "daily_send_time": config.default_daily_send_time,
            "check_interval_minutes": str(config.default_check_interval_minutes),
            "timezone": config.default_timezone,
        },
    )

    week_formula = fetch_week_formula(storage.get_setting(conn, "weektype_url"))
    week_formula_holder = WeekFormulaHolder(formula=week_formula)

    application = Application.builder().token(config.telegram_token).build()
    application.bot_data["conn"] = conn
    application.bot_data["admin_chat_id"] = config.admin_chat_id
    application.bot_data["group_name"] = storage.get_setting(conn, "group_name")
    application.bot_data["week_formula"] = week_formula_holder.formula
    application.bot_data["active_schedule_getter"] = lambda: _load_active_schedule(conn)
    application.bot_data["active_pdf_path_getter"] = lambda: storage.get_active_version(
        conn, storage.get_setting(conn, "group_name")
    ).pdf_cache_path

    async def sync_fn():
        pdf_source = storage.get_setting(conn, "pdf_source")
        group_name = storage.get_setting(conn, "group_name")

        async def notify(chat_id, text):
            await application.bot.send_message(chat_id=chat_id, text=text)

        return await check_and_apply_diff(conn, pdf_source, group_name, config.pdf_cache_dir, notify)

    application.bot_data["sync_fn"] = sync_fn

    scheduler_holder: dict = {}

    def reschedule_daily():
        reschedule_daily_job(scheduler_holder["scheduler"], conn, storage.get_setting(conn, "timezone"))

    def reschedule_hourly():
        reschedule_hourly_job(scheduler_holder["scheduler"], conn)

    application.bot_data["reschedule_daily_job"] = reschedule_daily
    application.bot_data["reschedule_hourly_job"] = reschedule_hourly

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("schedule", schedule_command))
    application.add_handler(CallbackQueryHandler(day_callback, pattern=r"^day:\d+$"))
    application.add_handler(CommandHandler("set_pdf_source", set_pdf_source_command))
    application.add_handler(CommandHandler("set_group", set_group_command))
    application.add_handler(CommandHandler("set_send_time", set_send_time_command))
    application.add_handler(CommandHandler("set_check_interval", set_check_interval_command))
    application.add_handler(CommandHandler("refresh", refresh_command))
    application.add_handler(CommandHandler("alert", alert_command))

    scheduler_holder["scheduler"] = build_scheduler(conn, config, application.bot, week_formula_holder)

    return application


def _load_active_schedule(conn):
    import json

    from schedule_bot.pdf_parser import schedule_from_dict

    group_name = storage.get_setting(conn, "group_name")
    active = storage.get_active_version(conn, group_name)
    if active is None:
        return None
    return schedule_from_dict(json.loads(active.payload_json))


if __name__ == "__main__":
    import sqlite3

    config = load_config(os.environ)
    connection = sqlite3.connect(config.db_path)
    storage.init_db(connection)
    app = build_application(config, connection)
    app.run_polling()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_main_wiring.py -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Run the full test suite**

Run: `pytest -v`
Expected: PASS (all tests across all tasks)

- [ ] **Step 6: Commit**

```bash
git add main.py tests/test_main_wiring.py
git commit -m "feat: wire application entrypoint with handlers, scheduler, and default settings"
```

---

## Task 15: Docker packaging and local smoke test

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`
- Create: `.dockerignore`

**Interfaces:**
- Consumes: `requirements.txt`, `main.py`, `schedule_bot/` (all prior tasks)
- Produces: a runnable container image

- [ ] **Step 1: Write `Dockerfile`**

```dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY schedule_bot/ ./schedule_bot/
COPY main.py .

RUN mkdir -p /app/data/pdf_cache

CMD ["python", "main.py"]
```

- [ ] **Step 2: Write `docker-compose.yml`**

```yaml
services:
  schedule-bot:
    build: .
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./data:/app/data
```

- [ ] **Step 3: Write `.dockerignore`**

```
.git
.venv
__pycache__
*.pyc
tests
docs
data
```

- [ ] **Step 4: Build the image locally to verify it compiles**

Run: `docker compose build`
Expected: build succeeds with no errors

- [ ] **Step 5: Manual local smoke test using a real `.env` and the fixture PDF**

This step is manual (not automated) — it exercises real Telegram I/O, which the rest of the plan deliberately avoided hitting in tests.

1. Copy `.env.example` to `.env` and fill in a real `TELEGRAM_BOT_TOKEN` (from @BotFather) and your own Telegram user ID as `ADMIN_CHAT_ID`.
2. Set `PDF_SOURCE=./tests/fixtures/schedule_sample.pdf` in `.env` to test against the local fixture instead of the network.
3. Run: `docker compose up`
4. In Telegram, message the bot `/start`, then `/schedule`, click a day button (e.g. "Ср") — confirm you receive both a text message and a photo.
5. As the admin, run `/refresh` — confirm it replies "Расписание загружено впервые." on the very first run, and "Изменений нет." on a second `/refresh`.
6. As the admin, run `/set_send_time 07:30` and `/set_check_interval 5` — confirm both reply with confirmation text.
7. As the admin, run `/alert Тестовое сообщение` — confirm subscribed chats receive it.
8. Stop the container: `docker compose down`.

- [ ] **Step 6: Commit**

```bash
git add Dockerfile docker-compose.yml .dockerignore
git commit -m "feat: add Docker packaging for deployment"
```

---

## Self-Review Notes

- **Spec coverage:** PDF parsing incl. merged-cell geometry (Task 5), week-type formula scraping (Task 3), diffing (Task 7), day screenshot (Task 8), sqlite schema incl. forward-compatible `group_name` columns (Task 2), hourly + daily jobs with runtime-reschedulable settings (Task 13), `/schedule` day picker (Task 11), admin commands incl. `/refresh` and `/alert` (Task 12), configurable `pdf_source` (URL or local path) (Task 4), Docker deployment (Task 15) — all present.
- **Fixture provenance:** `tests/fixtures/schedule_sample.pdf` is the real, currently-published PDF; every expected value in Tasks 4-6, 8 was derived by manually tracing pdfplumber's actual output for that exact file (documented inline in the design spec), not invented.
- **Type consistency:** verified `Lesson`, `DaySchedule`, `Schedule`, `RawRow`, `Cell` field names and constructor signatures are used identically across Tasks 4-13 (e.g. `Schedule.days`, `Lesson.upper`/`lower`, `WeekFormula.compute`).

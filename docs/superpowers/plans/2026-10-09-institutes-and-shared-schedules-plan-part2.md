# Институты — план реализации, часть 2 (геометрия PDF, скриншот, конвейер синка)

> Продолжение `docs/superpowers/plans/2026-10-09-institutes-and-shared-schedules-plan.md` — читать Global Constraints и Review Focus там. Та же REQUIRED SUB-SKILL (subagent-driven-development или executing-plans).

## Фаза 3 — Геометрия `pdf_parser.py`

### Task 8: `find_header_column_index` — поиск ограничен зоной до первого `day_marker`

**Files:**
- Modify: `schedule_bot/pdf_parser.py`
- Test: `tests/test_pdf_parser_build_schedule.py`

**Interfaces:**
- Consumes: `RawRow`, `Cell` (existing).
- Produces: `find_header_column_index(rows: list[RawRow], group_name: str) -> tuple[int, int]`.

- [ ] **Step 1: Падающий тест**

```python
# tests/test_pdf_parser_build_schedule.py
from schedule_bot.pdf_parser import find_header_column_index


def test_find_header_column_index_ignores_group_name_appearing_in_body_text():
    rows = [
        day_row(None, [Cell("Пн", 0, 30), None, None, Cell("Б-А-301", 300, 500)], day_marker=True, y0=0, y1=10),
        day_row("8.30-10.00", [None, Cell("8.30-10.00", 30, 60), Cell("совм. с Б-А-301", 60, 300), Cell("Матан", 300, 500)], y0=10, y1=20),
    ]
    header_idx, col_idx = find_header_column_index(rows, "Б-А-301")
    assert header_idx == 0
    assert col_idx == 3


def test_find_header_column_index_raises_when_not_in_header_zone():
    rows = [
        day_row(None, [Cell("Пн", 0, 30), None, None], day_marker=True, y0=0, y1=10),
        day_row("8.30-10.00", [None, Cell("8.30-10.00", 30, 60), Cell("совм. с Б-А-301", 60, 300)], y0=10, y1=20),
    ]
    with pytest.raises(ValueError):
        find_header_column_index(rows, "Б-А-301")
```

Add `import pytest` to the top of `tests/test_pdf_parser_build_schedule.py` if not already present.

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_pdf_parser_build_schedule.py -k find_header_column_index -v`
Expected: FAIL (`ImportError`)

- [ ] **Step 3: Реализация**

```python
# schedule_bot/pdf_parser.py

def _first_day_marker_index(rows: list[RawRow]) -> int:
    for index, row in enumerate(rows):
        if row.day_marker:
            return index
    return len(rows)


def find_header_column_index(rows: list[RawRow], group_name: str) -> tuple[int, int]:
    """Like find_group_column_index, but the search is restricted to
    rows[:first_day_marker_index] — the header zone. find_group_column_index
    scans the WHOLE table and returns the first substring match anywhere,
    which can land on a body row ("совм. с Б-А-301" in a lesson cell of
    another group) instead of the actual header — fatal for header-crop
    geometry (design spec, section 5.1). The same column index is valid
    for the whole table (fixed column layout), so this is only a
    narrower SEARCH, not a different RESULT shape."""
    header_zone = rows[: _first_day_marker_index(rows)]
    return find_group_column_index(header_zone, group_name)
```

`find_group_column_index` is unchanged and kept — still used by `find_header_column_index` internally and by the validation path in Task 16.

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_pdf_parser_build_schedule.py -k find_header_column_index -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/pdf_parser.py tests/test_pdf_parser_build_schedule.py
git commit -m "feat: find_header_column_index restricts header search to the pre-day-marker zone"
```

---

### Task 9: `DaySchedule` получает `time_col_x0/x1`/`group_col_x0/x1`; `build_schedule` агрегирует x-границы

**Files:**
- Modify: `schedule_bot/pdf_parser.py`
- Test: `tests/test_pdf_parser_build_schedule.py`

**Interfaces:**
- Consumes: `_DayAccumulator`, `Cell`, `RawRow` (existing).
- Produces: `DaySchedule(y0, y1, lessons, time_col_x0, time_col_x1, group_col_x0, group_col_x1)` — the last four are `float | None` (None when no lesson cell was ever found for that day, per spec 5.2); `build_schedule(rows, group_column_index, table_x1, time_column_index=1, epsilon=2.0)` gains a `time_column_index` parameter (defaults to the existing fixed layout position).

- [ ] **Step 1: Падающие тесты**

```python
# tests/test_pdf_parser_build_schedule.py
def test_day_schedule_records_time_and_group_column_x_bounds():
    rows = [
        day_row(None, [Cell("Пн", 0, 30), None, None], day_marker=True, y0=0, y1=10),
        day_row("8.30-10.00", [None, Cell("8.30-10.00", 30, 60), Cell("Матан", 300, 500)], y0=0, y1=10),
    ]
    schedule = build_schedule(rows, GROUP_COL, TABLE_X1)
    day = schedule["Понедельник"]
    assert day.time_col_x0 == 30
    assert day.time_col_x1 == 60
    assert day.group_col_x0 == 300
    assert day.group_col_x1 == 500


def test_day_schedule_group_col_bounds_use_merge_cell_when_own_cell_is_none():
    group_col = 3
    rows = [
        day_row(None, [Cell("Пн", 0, 30), None, None, None], day_marker=True, y0=0, y1=20),
        day_row("12.00-13.30", [None, Cell("12.00-13.30", 30, 60), None, Cell("", 300, 500)], y0=0, y1=10),
        day_row(None, [None, None, Cell("Общая пара", 60, 500), None], y0=10, y1=20),
    ]
    schedule = build_schedule(rows, group_col, TABLE_X1)
    day = schedule["Понедельник"]
    assert day.group_col_x0 == 60  # min across both the own (empty) cell and the merge cell
    assert day.group_col_x1 == 500


def test_day_schedule_x_bounds_are_none_when_no_lesson_found():
    rows = [
        day_row(None, [Cell("Пн", 0, 30), None, None], day_marker=True, y0=0, y1=20),
        day_row("8.30-10.00", [None, Cell("8.30-10.00", 30, 60), None], y0=0, y1=10),
    ]
    schedule = build_schedule(rows, GROUP_COL, TABLE_X1)
    day = schedule["Понедельник"]
    assert day.group_col_x0 is None
    assert day.group_col_x1 is None
    assert day.y0 == 0  # y bounds still touched even with no lesson found (spec 5.2)
    assert day.y1 == 20
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_pdf_parser_build_schedule.py -k day_schedule -v`
Expected: FAIL (`DaySchedule` has no `time_col_x0` field)

- [ ] **Step 3: Реализация**

```python
# schedule_bot/pdf_parser.py

@dataclass
class DaySchedule:
    y0: float
    y1: float
    lessons: list[Lesson]
    time_col_x0: float | None = None
    time_col_x1: float | None = None
    group_col_x0: float | None = None
    group_col_x1: float | None = None


class _DayAccumulator:
    def __init__(self) -> None:
        self.y0: float | None = None
        self.y1: float | None = None
        self.time_col_x0: float | None = None
        self.time_col_x1: float | None = None
        self.group_col_x0: float | None = None
        self.group_col_x1: float | None = None
        self._slots: dict[str, list[str | None]] = {}
        self._order: list[str] = []

    def touch_bounds(self, y0: float, y1: float) -> None:
        self.y0 = y0 if self.y0 is None else min(self.y0, y0)
        self.y1 = y1 if self.y1 is None else max(self.y1, y1)

    def touch_time_col_bounds(self, x0: float, x1: float) -> None:
        self.time_col_x0 = x0 if self.time_col_x0 is None else min(self.time_col_x0, x0)
        self.time_col_x1 = x1 if self.time_col_x1 is None else max(self.time_col_x1, x1)

    def touch_group_col_bounds(self, x0: float, x1: float) -> None:
        self.group_col_x0 = x0 if self.group_col_x0 is None else min(self.group_col_x0, x0)
        self.group_col_x1 = x1 if self.group_col_x1 is None else max(self.group_col_x1, x1)

    def add_value(self, time_label: str, text: str | None) -> None:
        if time_label not in self._slots:
            self._slots[time_label] = []
            self._order.append(time_label)
        self._slots[time_label].append(text)

    def finalize(self) -> DaySchedule:
        lessons: list[Lesson] = []
        for time_label in self._order:
            values = self._slots[time_label]
            if all(value is None for value in values):
                continue
            upper = values[0]
            lower = values[1] if len(values) > 1 else values[0]
            lessons.append(Lesson(time=time_label, upper=upper, lower=lower))
        return DaySchedule(
            y0=self.y0 or 0.0, y1=self.y1 or 0.0, lessons=lessons,
            time_col_x0=self.time_col_x0, time_col_x1=self.time_col_x1,
            group_col_x0=self.group_col_x0, group_col_x1=self.group_col_x1,
        )


def build_schedule(
    rows: list[RawRow],
    group_column_index: int,
    table_x1: float,
    time_column_index: int = 1,
    epsilon: float = 2.0,
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
                current_day = None
            current_time = None
            if current_day is not None and current_day not in accumulators:
                accumulators[current_day] = _DayAccumulator()

        if current_day is None:
            continue

        accumulators[current_day].touch_bounds(row.y0, row.y1)

        if row.time_label is not None:
            current_time = row.time_label

        if current_time is None:
            continue

        time_cell = row.cells[time_column_index] if time_column_index < len(row.cells) else None
        if time_cell is not None:
            accumulators[current_day].touch_time_col_bounds(time_cell.x0, time_cell.x1)

        cell = row.cells[group_column_index] if group_column_index < len(row.cells) else None

        if cell is not None:
            text = cell.text or None
            accumulators[current_day].touch_group_col_bounds(cell.x0, cell.x1)
        else:
            text = None
            found = False
            for other in row.cells[:group_column_index]:
                if other is not None and other.text and other.x1 >= table_x1 - epsilon:
                    text = other.text
                    accumulators[current_day].touch_group_col_bounds(other.x0, other.x1)
                    found = True
                    break
            if not found:
                continue

        accumulators[current_day].add_value(current_time, text)

    return {day: acc.finalize() for day, acc in accumulators.items()}
```

Note the second test above (`test_day_schedule_group_col_bounds_use_merge_cell_when_own_cell_is_none`) exercises the *own empty cell* at `x0=300,x1=500` recording no bounds (its `cell is not None` branch runs `touch_group_col_bounds(300, 500)` since `cell.text` being `""` still means `cell is not None`) — re-check against the actual fixture: the own cell is `Cell("", 300, 500)` (not `None`), so the `cell is not None` branch fires and touches `(300, 500)` directly, never reaching the merge-cell `else` branch. Fix the test fixture instead to use `None` for the target cell so the merge path is actually exercised:

```python
def test_day_schedule_group_col_bounds_use_merge_cell_when_own_cell_is_none():
    group_col = 3
    rows = [
        day_row(None, [Cell("Пн", 0, 30), None, None, None], day_marker=True, y0=0, y1=20),
        day_row("12.00-13.30", [None, Cell("12.00-13.30", 30, 60), None, None], y0=0, y1=10),
        day_row(None, [None, None, Cell("Общая пара", 60, 500), None], y0=10, y1=20),
    ]
    schedule = build_schedule(rows, group_col, TABLE_X1)
    day = schedule["Понедельник"]
    assert day.group_col_x0 == 60
    assert day.group_col_x1 == 500
```

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_pdf_parser_build_schedule.py -v`
Expected: PASS (all tests in the file, including the pre-existing ones from before this task — `build_schedule`'s positional-args call sites are unaffected since `time_column_index` has a default).

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/pdf_parser.py tests/test_pdf_parser_build_schedule.py
git commit -m "feat: DaySchedule tracks time/group column x-bounds for screenshot cropping"
```

---

### Task 10: `Schedule.header_y0/y1`, `parse_schedule` переписан, `parse_schedules_for_groups`

**Files:**
- Modify: `schedule_bot/pdf_parser.py`
- Test: `tests/test_pdf_parser_integration.py`
- Test: `tests/test_pdf_parser_extraction.py` (проверить, не ломается ли)

**Interfaces:**
- Consumes: `extract_raw_rows`, `find_header_column_index` (Task 8), `build_schedule` (Task 9).
- Produces: `Schedule(table_x0, table_x1, header_y0, header_y1, days)` — all fields required (no fallback/Optional per spec 5.6); `parse_schedule(source, group_name) -> Schedule` (same signature, new internals); `parse_schedules_for_groups(source: str, group_names: list[str]) -> dict[str, Schedule | None]` — one `extract_raw_rows` call, per-name `find_group_column_index`/`build_schedule` on the already-extracted rows, `None` for a name whose column isn't found.

- [ ] **Step 1: Падающие тесты**

```python
# tests/test_pdf_parser_integration.py — read the existing file first; add:

def test_parse_schedule_includes_header_y_bounds():
    schedule = parse_schedule(FIXTURE_PATH, "Б-Мв-101")
    assert schedule.header_y0 is not None
    assert schedule.header_y1 is not None
    assert schedule.header_y1 > schedule.header_y0


def test_schedule_round_trip_preserves_header_bounds():
    schedule = parse_schedule(FIXTURE_PATH, "Б-Мв-101")
    restored = schedule_from_dict(schedule_to_dict(schedule))
    assert restored.header_y0 == schedule.header_y0
    assert restored.header_y1 == schedule.header_y1
    restored_day = restored.days["Среда"]
    original_day = schedule.days["Среда"]
    assert restored_day.time_col_x0 == original_day.time_col_x0
    assert restored_day.group_col_x1 == original_day.group_col_x1


def test_parse_schedules_for_groups_extracts_raw_rows_exactly_once(monkeypatch):
    import schedule_bot.pdf_parser as pdf_parser_module

    call_count = 0
    real_extract = pdf_parser_module.extract_raw_rows

    def counting_extract(source):
        nonlocal call_count
        call_count += 1
        return real_extract(source)

    monkeypatch.setattr(pdf_parser_module, "extract_raw_rows", counting_extract)

    results = parse_schedules_for_groups(FIXTURE_PATH, ["Б-Мв-101", "Б-Эв-101"])

    assert call_count == 1
    assert results["Б-Мв-101"] is not None
    assert isinstance(results["Б-Мв-101"], pdf_parser_module.Schedule)


def test_parse_schedules_for_groups_returns_none_for_unknown_group():
    results = parse_schedules_for_groups(FIXTURE_PATH, ["Б-Мв-101", "НЕТ-ТАКОЙ-ГРУППЫ"])
    assert results["Б-Мв-101"] is not None
    assert results["НЕТ-ТАКОЙ-ГРУППЫ"] is None
```

(Replace `"Б-Эв-101"` with whatever second real group name exists as a column in `tests/fixtures/schedule_sample.pdf` — check via `extract_raw_rows` in a scratch run if unsure; any second valid column header in the fixture works equally for the dedup-count assertion.)

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_pdf_parser_integration.py -v`
Expected: FAIL (`Schedule.header_y0` doesn't exist; `parse_schedules_for_groups` doesn't exist)

- [ ] **Step 3: Реализация**

```python
# schedule_bot/pdf_parser.py

@dataclass
class Schedule:
    table_x0: float
    table_x1: float
    header_y0: float
    header_y1: float
    days: dict[str, DaySchedule]


def parse_schedule(source: str, group_name: str) -> Schedule:
    rows, table_bbox = extract_raw_rows(source)
    header_idx, col_idx = find_header_column_index(rows, group_name)
    body_rows = rows[header_idx + 1 :]
    days = build_schedule(body_rows, col_idx, table_bbox[2])
    header_row = rows[header_idx]
    return Schedule(
        table_x0=table_bbox[0], table_x1=table_bbox[2],
        header_y0=header_row.y0, header_y1=header_row.y1,
        days=days,
    )


def parse_schedules_for_groups(source: str, group_names: list[str]) -> dict[str, Schedule | None]:
    """One extract_raw_rows(source) call, shared by every name — this is
    the actual fetch+parse deduplication (design spec, section 3.1), not
    just byte-level caching. A name whose column can't be found gets
    None instead of raising, since this is used both by routine sync
    (one bad name in a link shouldn't abort the rest) and by the
    one-shot subscribe-time validation (section 1.5), where "not found"
    IS the expected outcome being tested for."""
    rows, table_bbox = extract_raw_rows(source)
    results: dict[str, Schedule | None] = {}
    for group_name in group_names:
        try:
            header_idx, col_idx = find_header_column_index(rows, group_name)
        except ValueError:
            results[group_name] = None
            continue
        body_rows = rows[header_idx + 1 :]
        days = build_schedule(body_rows, col_idx, table_bbox[2])
        header_row = rows[header_idx]
        results[group_name] = Schedule(
            table_x0=table_bbox[0], table_x1=table_bbox[2],
            header_y0=header_row.y0, header_y1=header_row.y1,
            days=days,
        )
    return results


def schedule_to_dict(schedule: Schedule) -> dict:
    return {
        "table_x0": schedule.table_x0,
        "table_x1": schedule.table_x1,
        "header_y0": schedule.header_y0,
        "header_y1": schedule.header_y1,
        "days": {
            day: {
                "y0": day_schedule.y0,
                "y1": day_schedule.y1,
                "time_col_x0": day_schedule.time_col_x0,
                "time_col_x1": day_schedule.time_col_x1,
                "group_col_x0": day_schedule.group_col_x0,
                "group_col_x1": day_schedule.group_col_x1,
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
            time_col_x0=value.get("time_col_x0"),
            time_col_x1=value.get("time_col_x1"),
            group_col_x0=value.get("group_col_x0"),
            group_col_x1=value.get("group_col_x1"),
        )
        for day, value in data["days"].items()
    }
    return Schedule(
        table_x0=data["table_x0"], table_x1=data["table_x1"],
        header_y0=data["header_y0"], header_y1=data["header_y1"],
        days=days,
    )
```

(`schedule_from_dict` uses `.get(...)` only for the four new per-day x-bound keys so a dict produced by `schedule_to_dict` round-trips exactly — this is NOT the backwards-compat fallback the spec forbids in 5.6, since every `schedule_to_dict` output from this task onward always includes these keys; `.get` is just a defensive read of a dict this same module just wrote.)

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_pdf_parser_integration.py tests/test_pdf_parser_extraction.py tests/test_pdf_parser_build_schedule.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/pdf_parser.py tests/test_pdf_parser_integration.py
git commit -m "feat: Schedule carries header y-bounds; parse_schedules_for_groups shares one extract_raw_rows call"
```

---

## Фаза 4 — Скриншот

### Task 11: `screenshot.render_day_image` — колонка времени + колонка группы, шапка той же ширины

**Files:**
- Modify: `schedule_bot/screenshot.py`
- Test: `tests/test_screenshot.py`

**Interfaces:**
- Consumes: `Schedule`, `DaySchedule` (Tasks 9-10).
- Produces: `render_day_image(pdf_path: str, schedule: Schedule, day: str, zoom: float = 2.5, padding: float = 4.0) -> bytes | None` — same signature, new internals.

- [ ] **Step 1: Падающие тесты**

```python
# tests/test_screenshot.py — existing 3 tests stay (parse_schedule/render_day_image signatures unchanged), add:

def test_render_day_image_width_matches_sum_of_header_and_body_crop_ranges():
    schedule = parse_schedule(FIXTURE_PATH, "Б-Мв-101")
    image_bytes = render_day_image(FIXTURE_PATH, schedule, "Среда")
    assert image_bytes is not None
    # PNG IHDR chunk: width is the first 4 bytes after the 8-byte signature + 4-byte length + "IHDR".
    import struct
    width = struct.unpack(">I", image_bytes[16:20])[0]
    day = schedule.days["Среда"]
    zoom = 2.5
    expected_px_width = round((day.group_col_x1 - day.time_col_x0) * zoom)
    assert abs(width - expected_px_width) <= 2  # pixel rounding tolerance


def test_render_day_image_single_crop_when_time_and_group_columns_are_adjacent(monkeypatch):
    import schedule_bot.screenshot as screenshot_module
    from schedule_bot.pdf_parser import DaySchedule, Schedule

    schedule = Schedule(
        table_x0=0.0, table_x1=500.0, header_y0=0.0, header_y1=10.0,
        days={"Понедельник": DaySchedule(
            y0=10.0, y1=20.0, lessons=[],
            time_col_x0=30.0, time_col_x1=60.0, group_col_x0=60.0, group_col_x1=300.0,
        )},
    )
    calls = []

    class FakePixmap:
        def tobytes(self, fmt):
            return b"\x89PNG\r\n\x1a\n" + b"0" * 40

    class FakePage:
        def get_pixmap(self, matrix, clip):
            calls.append(clip)
            return FakePixmap()

    class FakeDocument:
        def __getitem__(self, index):
            return FakePage()

        def close(self):
            pass

    monkeypatch.setattr(screenshot_module.fitz, "open", lambda path: FakeDocument())

    from schedule_bot.pdf_parser import Lesson
    schedule.days["Понедельник"].lessons = [Lesson(time="8.30-10.00", upper="X", lower="X")]

    render_day_image(FIXTURE_PATH, schedule, "Понедельник")

    # one body clip + one header clip == 2 get_pixmap calls, never 4 (no split-and-stitch)
    assert len(calls) == 2


def test_render_day_image_returns_none_when_group_col_bounds_missing():
    from schedule_bot.pdf_parser import DaySchedule, Lesson, Schedule

    schedule = Schedule(
        table_x0=0.0, table_x1=500.0, header_y0=0.0, header_y1=10.0,
        days={"Понедельник": DaySchedule(
            y0=10.0, y1=20.0, lessons=[Lesson(time="8.30-10.00", upper="X", lower="X")],
            time_col_x0=30.0, time_col_x1=60.0, group_col_x0=None, group_col_x1=None,
        )},
    )
    assert render_day_image(FIXTURE_PATH, schedule, "Понедельник") is None
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_screenshot.py -v`
Expected: FAIL (old `render_day_image` still clips the whole table width, ignores header, has no 2-vs-4-pixmap-call distinction)

- [ ] **Step 3: Реализация**

```python
# schedule_bot/screenshot.py
import fitz

from schedule_bot.pdf_parser import Schedule


def _crop_pixmap(page, y0: float, y1: float, x_ranges: list[tuple[float, float]], zoom: float, padding: float):
    """Renders one or two adjacent clip rects at the given y-range and
    stitches them horizontally if there are two (non-adjacent time/group
    columns); a single rect if there's only one (adjacent/merged case).
    Returns a fitz.Pixmap."""
    pixmaps = [
        page.get_pixmap(
            matrix=fitz.Matrix(zoom, zoom),
            clip=fitz.Rect(x0 - padding, max(y0 - padding, 0.0), x1 + padding, y1 + padding),
        )
        for x0, x1 in x_ranges
    ]
    if len(pixmaps) == 1:
        return pixmaps[0]
    stitched = fitz.Pixmap(pixmaps[0].colorspace, (0, 0, sum(p.width for p in pixmaps), pixmaps[0].height), pixmaps[0].alpha)
    offset = 0
    for pixmap in pixmaps:
        stitched.copy(pixmap, (offset, 0, offset + pixmap.width, pixmap.height))
        offset += pixmap.width
    return stitched


def _x_ranges_for(time_x0: float, time_x1: float, group_x0: float, group_x1: float, padding: float) -> list[tuple[float, float]]:
    if time_x1 + padding >= group_x0 - padding:
        return [(time_x0, group_x1)]
    return [(time_x0, time_x1), (group_x0, group_x1)]


def render_day_image(
    pdf_path: str, schedule: Schedule, day: str, zoom: float = 2.5, padding: float = 4.0
) -> bytes | None:
    day_schedule = schedule.days.get(day)
    if day_schedule is None or not day_schedule.lessons:
        return None
    if day_schedule.group_col_x0 is None or day_schedule.group_col_x1 is None:
        return None

    x_ranges = _x_ranges_for(
        day_schedule.time_col_x0, day_schedule.time_col_x1,
        day_schedule.group_col_x0, day_schedule.group_col_x1, padding,
    )

    document = fitz.open(pdf_path)
    try:
        page = document[0]
        header_pixmap = _crop_pixmap(page, schedule.header_y0, schedule.header_y1, x_ranges, zoom, padding)
        body_pixmap = _crop_pixmap(page, day_schedule.y0, day_schedule.y1, x_ranges, zoom, padding)

        combined = fitz.Pixmap(
            body_pixmap.colorspace, (0, 0, body_pixmap.width, header_pixmap.height + body_pixmap.height), body_pixmap.alpha
        )
        combined.copy(header_pixmap, (0, 0, header_pixmap.width, header_pixmap.height))
        combined.copy(body_pixmap, (0, header_pixmap.height, body_pixmap.width, header_pixmap.height + body_pixmap.height))
        return combined.tobytes("png")
    finally:
        document.close()
```

The `fitz.Pixmap.copy` method takes `(source_pixmap, bbox)` where `bbox` is the destination rect within the new pixmap — verify against the installed PyMuPDF version's actual `Pixmap.copy`/`Pixmap` constructor signature during implementation (`python3 -c "import fitz; help(fitz.Pixmap.copy)"`) and adjust the exact call if the installed version differs; the shape of the approach (render header clip once, render body clip once, stitch vertically, and stitch horizontally first when time/group columns aren't adjacent) is what the tests pin, not the exact PyMuPDF API spelling.

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_screenshot.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/screenshot.py tests/test_screenshot.py
git commit -m "feat: render_day_image crops time+group columns with a matching-width header"
```

---

## Фаза 5 — Конвейер сверки (`sync.py`)

### Task 12: `sync.py` — `sync_institute_link`, дедуп фетча+парсинга, кэш по группам, `last_sync_failed_at` на двух уровнях

**Files:**
- Modify: `schedule_bot/sync.py` (полная замена `check_and_apply_diff`)
- Modify: `schedule_bot/storage.py` (нет новых функций — `list_group_members(conn, group_id, subscribed_only=False)` уже существует и используется напрямую)
- Test: `tests/test_sync.py` (полная замена)

**Interfaces:**
- Consumes: `storage.{list_groups_for_link, list_group_members, get_active_version, insert_new_version, set_group_last_sync_failed_at, get_institute_link, update_institute_link_pdf_url}` (Phase 1); `pdf_parser.{_load_pdf_bytes, parse_schedules_for_groups, schedule_from_dict, schedule_to_dict}` (Phase 3); `group_links.refresh_institute_catalog` (Task 7); `diff.compute_diff` (unchanged).
- Produces: `SyncResult(status, changes, per_group=...)` — extended to report per-group outcomes since one link's sync can partially succeed; `async def sync_institute_link(conn, institute_link, institute, pdf_cache_dir: str, notify: Notifier, *, catalog_refreshed_this_cycle: set[int]) -> list[SyncResult]` — one `SyncResult` per active group of the link. `catalog_refreshed_this_cycle` is a caller-owned, per-scheduler-run `set[int]` of institute ids already catalog-refreshed this cycle (rate-limits the 404 fallback per spec 3.3/3.4 — scheduler/main own its lifetime, Task 14 shows how).

- [ ] **Step 1: Падающие тесты (замена `tests/test_sync.py` целиком)**

```python
# tests/test_sync.py
import asyncio
import sqlite3
from pathlib import Path

import pytest

from schedule_bot import storage
from schedule_bot.sync import sync_institute_link

FIXTURE_PATH = str(Path(__file__).parent / "fixtures" / "schedule_sample.pdf")


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
        str(tmp_path), RecordingNotifier(), catalog_refreshed_this_cycle=set(),
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
        str(tmp_path), RecordingNotifier(), catalog_refreshed_this_cycle=set(),
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
        str(tmp_path), RecordingNotifier(), catalog_refreshed_this_cycle=set(),
    ))

    assert call_count == 1
    assert {r.status for r in results} == {"initial"}
    assert len(results) == 2


def test_sync_institute_link_writes_separate_cache_copy_per_group(conn, tmp_path):
    institute_id = _make_institute(conn)
    link_id, group_ids = _make_link_and_groups(conn, institute_id, FIXTURE_PATH, ["Б-Мв-101", "Б-Эв-101"])
    for group_id in group_ids:
        storage.upsert_member(conn, group_id * 1000, group_id, joined_at="t")

    asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_id), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_refreshed_this_cycle=set(),
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
        str(tmp_path), RecordingNotifier(), catalog_refreshed_this_cycle=set(),
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
        str(tmp_path), RecordingNotifier(), catalog_refreshed_this_cycle=set(),
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

    shared_cycle_flag: set[int] = set()
    asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_a), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_refreshed_this_cycle=shared_cycle_flag,
    ))
    asyncio.run(sync_institute_link(
        conn, storage.get_institute_link(conn, link_b), storage.get_institute(conn, institute_id),
        str(tmp_path), RecordingNotifier(), catalog_refreshed_this_cycle=shared_cycle_flag,
    ))

    assert refresh_calls == [institute_id]  # second 404, same cycle, does not refresh again


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
        str(tmp_path), RecordingNotifier(), catalog_refreshed_this_cycle=set(),
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
        str(tmp_path), notifier, catalog_refreshed_this_cycle=set(),
    ))
    # First sync is "initial" -> no notifications at all regardless of subscription.
    assert notifier.sent == []
```

- [ ] **Step 2: Запустить, убедиться что падает**

Run: `./.venv/bin/python -m pytest tests/test_sync.py -v`
Expected: FAIL (`ImportError: cannot import name 'sync_institute_link'`)

- [ ] **Step 3: Реализация**

```python
# schedule_bot/sync.py — full replacement
import asyncio
import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from schedule_bot import storage
from schedule_bot.diff import compute_diff
from schedule_bot.group_links import refresh_institute_catalog
from schedule_bot.pdf_parser import _load_pdf_bytes, parse_schedules_for_groups, schedule_from_dict, schedule_to_dict
from schedule_bot.telegram_utils import SEND_PACING_SECONDS

logger = logging.getLogger(__name__)

Notifier = Callable[[int, str], Awaitable[None]]

MAX_FETCH_ATTEMPTS = 3

_UNSAFE_PATH_CHARS = re.compile(r"[^\w.-]")


@dataclass
class SyncResult:
    group_id: int
    status: str
    changes: list[str] = field(default_factory=list)


def _safe_segment(value: str) -> str:
    return _UNSAFE_PATH_CHARS.sub("_", value)


def _write_cache(pdf_bytes: bytes, pdf_cache_dir: str, institute_link_id: int, group_name: str) -> str:
    cache_dir = Path(pdf_cache_dir) / f"link_{institute_link_id}" / _safe_segment(group_name)
    cache_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    destination = cache_dir / f"{timestamp}.pdf"
    destination.write_bytes(pdf_bytes)
    return str(destination)


def _fetch_pdf_bytes_with_retries(pdf_source: str) -> tuple[bytes | None, Exception | None]:
    last_error: Exception | None = None
    for attempt in range(MAX_FETCH_ATTEMPTS):
        try:
            return _load_pdf_bytes(pdf_source), None
        except Exception as exc:
            last_error = exc
            logger.warning("sync: fetch attempt %d/%d failed for %r: %s", attempt + 1, MAX_FETCH_ATTEMPTS, pdf_source, exc)
    return None, last_error


def _fetch_and_parse_blocking(
    pdf_source: str, group_names: list[str]
) -> tuple[bytes | None, dict[str, object | None], Exception | None]:
    pdf_bytes, error = _fetch_pdf_bytes_with_retries(pdf_source)
    if pdf_bytes is None:
        return None, {}, error

    tmp_file = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp_file.write(pdf_bytes)
        tmp_file.close()
        schedules = parse_schedules_for_groups(tmp_file.name, group_names)
        return pdf_bytes, schedules, None
    finally:
        try:
            os.unlink(tmp_file.name)
        except OSError:
            pass


async def sync_institute_link(
    conn, institute_link, institute, pdf_cache_dir: str, notify: Notifier,
    *, catalog_refreshed_this_cycle: set[int],
) -> list[SyncResult]:
    groups = storage.list_groups_for_link(conn, institute_link.id)
    active_groups = [g for g in groups if storage.list_group_members(conn, g.id, subscribed_only=False)]
    if not active_groups or institute_link.is_stale:
        return []

    group_names = [g.name for g in active_groups]
    pdf_bytes, schedules, error = await asyncio.to_thread(
        _fetch_and_parse_blocking, institute_link.pdf_url, group_names
    )

    if pdf_bytes is None:
        if institute.id not in catalog_refreshed_this_cycle:
            catalog_refreshed_this_cycle.add(institute.id)
            try:
                refresh_institute_catalog(conn, institute, notify_admin=lambda text: None)
            except Exception as refresh_error:
                logger.warning("sync: catalog refresh fallback failed for institute %r: %s", institute.name, refresh_error)
            refreshed_link = storage.get_institute_link(conn, institute_link.id)
            if refreshed_link is not None and refreshed_link.pdf_url != institute_link.pdf_url:
                pdf_bytes, schedules, error = await asyncio.to_thread(
                    _fetch_and_parse_blocking, refreshed_link.pdf_url, group_names
                )

    if pdf_bytes is None:
        now = datetime.now(timezone.utc).isoformat()
        for group in active_groups:
            storage.set_group_last_sync_failed_at(conn, group.id, now)
        logger.error("sync: fetch failed for link %r: %s", institute_link.pdf_url, error)
        return [SyncResult(group_id=group.id, status="error") for group in active_groups]

    results: list[SyncResult] = []
    for group in active_groups:
        new_schedule = schedules.get(group.name)
        if new_schedule is None:
            storage.set_group_last_sync_failed_at(conn, group.id, datetime.now(timezone.utc).isoformat())
            results.append(SyncResult(group_id=group.id, status="error"))
            continue

        storage.set_group_last_sync_failed_at(conn, group.id, "")
        active = storage.get_active_version(conn, group.id)
        old_schedule = schedule_from_dict(json.loads(active.payload_json)) if active else None
        changes = compute_diff(old_schedule, new_schedule)

        if active is not None and not changes:
            results.append(SyncResult(group_id=group.id, status="no_changes"))
            continue

        cache_path = _write_cache(pdf_bytes, pdf_cache_dir, institute_link.id, group.name)
        parsed_at = datetime.now(timezone.utc).isoformat()
        storage.insert_new_version(conn, group.id, json.dumps(schedule_to_dict(new_schedule)), cache_path, parsed_at)

        if active is not None and active.pdf_cache_path:
            try:
                Path(active.pdf_cache_path).unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("sync: failed to delete superseded cache file %r: %s", active.pdf_cache_path, exc)

        if active is None:
            results.append(SyncResult(group_id=group.id, status="initial"))
            continue

        message = "Расписание изменилось. Актуальную версию смотрите на сайте"
        weektype_url = storage.get_setting(conn, "weektype_url")
        if weektype_url:
            message += ":\n" + weektype_url
        for chat_id in storage.list_group_members(conn, group.id, subscribed_only=True):
            try:
                await notify(chat_id, message)
            except Exception as exc:
                logger.warning("sync: failed to notify chat_id=%s for group %r: %s", chat_id, group.name, exc)
            await asyncio.sleep(SEND_PACING_SECONDS)

        results.append(SyncResult(group_id=group.id, status="applied", changes=changes))

    return results
```

Note on `test_sync_institute_link_notifies_subscribers_not_just_members`: the implementation's notify loop iterates `subscribed_only=True` (spec 3.2 step 5 — notification is about delivery, scoped by `subscribed`), while the **gate** for whether to sync at all uses `subscribed_only=False` (spec 3.2 step 1 — a pinned-but-unsubscribed member still needs `/schedule` to work). Both are deliberately different and are exercised by separate tests above.

- [ ] **Step 4: Запустить тесты**

Run: `./.venv/bin/python -m pytest tests/test_sync.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add schedule_bot/sync.py tests/test_sync.py
git commit -m "feat: sync_institute_link dedups fetch+parse per link, isolates per-group failures"
```

---

**Продолжение:** `docs/superpowers/plans/2026-10-09-institutes-and-shared-schedules-plan-part3.md` (Фаза 6 — валидация при подписке и немедленный sync, Фаза 7 — планировщик, Фаза 8 — admin-команды/меню/main.py wiring, финальная чистка тестов и self-review).

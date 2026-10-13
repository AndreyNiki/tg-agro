import io
import re
from dataclasses import asdict, dataclass

import pdfplumber

# A body (lesson) row is identified by an actual time-range label
# ("8.30-10.00", "13.40-15.10"); header rows carry either no label or a
# caption like "Часы". Used to find the end of the header zone.
_TIME_LABEL_RE = re.compile(r"^\s*\d{1,2}[.:]\d{2}\s*[-–—]\s*\d{1,2}[.:]\d{2}")


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
        from schedule_bot.browser_client import fetch_url, site_root

        response = fetch_url(source, timeout=30, referer=site_root(source))
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


def _header_zone_end_index(rows: list[RawRow]) -> int:
    """Index of the first BODY (lesson) row — i.e. the exclusive end of
    the table's header zone.

    `row.day_marker` alone cannot express this boundary: it is derived
    from `cells[0] is not None`, and `cells[0]` (the day column) is
    non-None both for the real header rows AND for the row where each
    day's label first appears — which in the real fixture
    (`tests/fixtures/schedule_sample.pdf`) is Monday's FIRST lesson row
    (row index 2, time label "8.30-10.00"). Stopping at the first
    `day_marker is False` row therefore left that lesson row inside the
    "header zone", re-opening exactly the false-positive this zone exists
    to prevent (a group name mentioned in lesson text, e.g.
    "совм. с Б-А-301" — design spec 5.1).

    The reliable marker of a body row is an actual time-range label
    ("8.30-10.00"): header rows either have no time label at all or a
    non-range caption ("Часы"). We stop at the first such row; the
    `day_marker is False` condition is kept only as a secondary guard for
    tables whose body rows carry no time label at all."""
    for index, row in enumerate(rows):
        if row.time_label and _TIME_LABEL_RE.match(row.time_label):
            return index
        if not row.day_marker:
            return index
    return len(rows)


def find_header_column_index(rows: list[RawRow], group_name: str) -> tuple[int, int]:
    """Like find_group_column_index, but the search is restricted to the
    header zone (`rows[:_header_zone_end_index(rows)]`).
    find_group_column_index scans the WHOLE table and returns the first
    substring match anywhere, which can land on a body row ("совм. с
    Б-А-301" in a lesson cell of another group) instead of the actual
    header — fatal for header-crop geometry (design spec, section 5.1).
    The same column index is valid for the whole table (fixed column
    layout), so this is only a narrower SEARCH, not a different RESULT
    shape."""
    header_zone = rows[: _header_zone_end_index(rows)]
    return find_group_column_index(header_zone, group_name)


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


_DAY_ABBREVIATIONS = {"Пн": "Понедельник", "Вт": "Вторник", "Ср": "Среда", "Чт": "Четверг", "Пт": "Пятница", "Сб": "Суббота"}


def _match_day(day_text: str | None) -> str | None:
    """Matches a day_marker row's cells[0] text against DAY_ORDER by
    name, not by position — a row's position among day_marker rows is
    not reliable evidence it's a real day label (e.g. an extra
    sub-header row like "Часы/1 подгр/2 подгр" also has cells[0] not
    None). The real PDF renders this cell's day name VERTICALLY, so
    pdfplumber's extraction comes back as one character/chunk per line,
    top-to-bottom — which is the day name's letters in REVERSE reading
    order (e.g. "Понедельник" extracts as "л\\nнеде\\nПо"). Reversing the
    line order before matching recovers the (possibly cell-height
    truncated, hence prefix-matched) real name; a short synthetic test
    label ("Пн") is handled via the abbreviation map instead."""
    if not day_text:
        return None
    normalized = "".join(reversed(day_text.split("\n"))).strip()
    if not normalized:
        return None
    for day in DAY_ORDER:
        if day.startswith(normalized):
            return day
    return _DAY_ABBREVIATIONS.get(normalized)


def build_schedule(
    rows: list[RawRow],
    group_column_index: int,
    table_x1: float,
    time_column_index: int = 1,
    epsilon: float = 2.0,
) -> dict[str, DaySchedule]:
    current_day: str | None = None
    current_time: str | None = None
    accumulators: dict[str, _DayAccumulator] = {}

    for row in rows:
        if row.day_marker:
            day_cell = row.cells[0] if row.cells else None
            matched_day = _match_day(day_cell.text if day_cell is not None else None)
            if matched_day is not None:
                current_day = matched_day
                current_time = None
                if current_day not in accumulators:
                    accumulators[current_day] = _DayAccumulator()
            # A day_marker row whose cells[0] text doesn't match any known
            # day name (a stray sub-header, or a trailing footer line) is
            # NOT a day transition — fall through and keep accumulating
            # into whichever day is already current (or skip, if none yet).

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


def parse_schedules_for_groups(source: str, group_names: list[str]) -> dict[str, "Schedule | None"]:
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

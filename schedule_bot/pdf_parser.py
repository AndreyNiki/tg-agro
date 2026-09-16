import io
from dataclasses import asdict, dataclass

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

    def touch_bounds(self, y0: float, y1: float) -> None:
        self.y0 = y0 if self.y0 is None else min(self.y0, y0)
        self.y1 = y1 if self.y1 is None else max(self.y1, y1)

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
        return DaySchedule(y0=self.y0 or 0.0, y1=self.y1 or 0.0, lessons=lessons)


def build_schedule(
    rows: list[RawRow],
    group_column_index: int,
    table_x1: float,
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

        cell = row.cells[group_column_index] if group_column_index < len(row.cells) else None

        if cell is not None:
            text = cell.text or None
        else:
            text = None
            found = False
            for other in row.cells[:group_column_index]:
                if other is not None and other.text and other.x1 >= table_x1 - epsilon:
                    text = other.text
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

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


def test_render_day_image_width_matches_sum_of_header_and_body_crop_ranges():
    # NOTE: in the real fixture, "Б-Мв-101"'s own column for "Среда" is not
    # adjacent to the time column (several other groups' columns sit
    # between them), so the correct cropped width is the SUM of the two
    # separately-rendered column widths (time column + target group
    # column), not (group_col_x1 - time_col_x0) * zoom, which would include
    # the excluded middle columns. We compute the expected width the same
    # way render_day_image does (render each column range once with fitz
    # and sum their pixel widths) rather than hardcoding a formula that
    # assumes adjacency.
    import fitz

    schedule = parse_schedule(FIXTURE_PATH, "Б-Мв-101")
    image_bytes = render_day_image(FIXTURE_PATH, schedule, "Среда")
    assert image_bytes is not None
    # PNG IHDR chunk: width is the first 4 bytes after the 8-byte signature + 4-byte length + "IHDR".
    import struct
    width = struct.unpack(">I", image_bytes[16:20])[0]
    day = schedule.days["Среда"]
    zoom = 2.5
    padding = 4.0

    document = fitz.open(FIXTURE_PATH)
    page = document[0]
    time_clip = fitz.Rect(
        day.time_col_x0 - padding, max(schedule.header_y0 - padding, 0.0),
        day.time_col_x1 + padding, schedule.header_y1 + padding,
    )
    group_clip = fitz.Rect(
        day.group_col_x0 - padding, max(schedule.header_y0 - padding, 0.0),
        day.group_col_x1 + padding, schedule.header_y1 + padding,
    )
    time_pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=time_clip)
    group_pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=group_clip)
    expected_px_width = time_pixmap.width + group_pixmap.width
    document.close()

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

    import fitz as _fitz

    class FakePage:
        def get_pixmap(self, matrix, clip):
            calls.append(clip)
            # A real, tiny fitz.Pixmap: valid for fitz.Pixmap(...)/copy()
            # calls downstream, independent of the (mocked) clip geometry.
            return _fitz.Pixmap(_fitz.csRGB, (0, 0, 10, 10), False)

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


def test_render_day_image_header_crop_x_range_matches_body_for_merged_cell_day(monkeypatch):
    """A day whose target-group lesson is shared/merged across columns (per
    Task 9's aggregation) has a wide group_col_x1 reaching the table's right
    edge. The header crop MUST use that exact same wide x-range, not a
    narrower range fitted to just the target group's own column."""
    import schedule_bot.screenshot as screenshot_module
    from schedule_bot.pdf_parser import DaySchedule, Lesson, Schedule

    # group_col_x1 == table_x1: this is what a merged/shared cell spanning
    # to the table's right edge looks like after Task 9's aggregation.
    schedule = Schedule(
        table_x0=0.0, table_x1=500.0, header_y0=0.0, header_y1=10.0,
        days={"Четверг": DaySchedule(
            y0=10.0, y1=20.0, lessons=[Lesson(time="8.30-10.00", upper="X", lower="X")],
            time_col_x0=30.0, time_col_x1=60.0, group_col_x0=60.0, group_col_x1=500.0,
        )},
    )
    clips = []

    import fitz as _fitz

    class FakePage:
        def get_pixmap(self, matrix, clip):
            clips.append(clip)
            return _fitz.Pixmap(_fitz.csRGB, (0, 0, 10, 10), False)

    class FakeDocument:
        def __getitem__(self, index):
            return FakePage()

        def close(self):
            pass

    monkeypatch.setattr(screenshot_module.fitz, "open", lambda path: FakeDocument())

    render_day_image(FIXTURE_PATH, schedule, "Четверг")

    # Exactly one header clip and one body clip (adjacent time/group columns).
    assert len(clips) == 2
    header_clip, body_clip = clips
    # Header x-range must exactly match body x-range, including the merged
    # cell's wide extent (group_col_x1 == table_x1 == 500.0), not a
    # narrower range computed independently for the header.
    assert (header_clip.x0, header_clip.x1) == (body_clip.x0, body_clip.x1)
    padding = 4.0
    assert header_clip.x1 == 500.0 + padding

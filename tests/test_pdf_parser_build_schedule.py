import pytest

from schedule_bot.pdf_parser import Cell, RawRow, Lesson, build_schedule, find_header_column_index

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
        day_row(None, [Cell("Пн", 0, 30), None, None], day_marker=True, y0=0, y1=20),
        day_row("10.10-11.40", [None, Cell("10.10-11.40", 30, 60), Cell("", 300, 500)], y0=0, y1=10),
        day_row(None, [None, None, Cell("Математика", 300, 500)], y0=10, y1=20),
    ]
    schedule = build_schedule(rows, GROUP_COL, TABLE_X1)
    lesson = schedule["Понедельник"].lessons[0]
    assert lesson.upper is None
    assert lesson.lower == "Математика"


def test_merged_cell_spanning_to_table_edge_applies_to_target_group():
    group_col = 3  # 4-column layout: day, time, other-group, target
    rows = [
        day_row(None, [Cell("Пн", 0, 30), None, None, None], day_marker=True, y0=0, y1=20),
        day_row("12.00-13.30", [None, Cell("12.00-13.30", 30, 60), None, Cell("", 300, 500)], y0=0, y1=10),
        day_row(None, [None, None, Cell("Общая пара для всех групп", 60, 500), None], y0=10, y1=20),
    ]
    schedule = build_schedule(rows, group_col, TABLE_X1)
    lesson = schedule["Понедельник"].lessons[0]
    assert lesson.upper is None
    assert lesson.lower == "Общая пара для всех групп"


def test_non_merged_empty_continuation_row_is_ignored_not_recorded():
    group_col = 3  # 4-column layout: day, time, other-group, target
    rows = [
        day_row(None, [Cell("Пн", 0, 30), None, None, None], day_marker=True, y0=0, y1=20),
        day_row("8.30-10.00", [None, Cell("8.30-10.00", 30, 60), None, Cell("Основы права", 300, 500)], y0=0, y1=10),
        day_row(None, [None, None, Cell("Другая группа занята", 60, 250), None], y0=10, y1=20),
    ]
    schedule = build_schedule(rows, group_col, TABLE_X1)
    lessons = schedule["Понедельник"].lessons
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
    day_labels = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб"]
    rows = []
    for label in day_labels:
        rows.append(
            day_row(None, [Cell(label, 0, 30), None, None], day_marker=True, y0=0, y1=10)
        )
        rows.append(
            day_row("8.30-10.00", [None, Cell("8.30-10.00", 30, 60), Cell("Урок", 300, 500)], y0=0, y1=10)
        )
    # a 7th "day marker" row, e.g. a footer signature line, must not be
    # mistaken for a day label, must not crash/extend the result, and —
    # since its cell spans to the table's right edge, exactly like a real
    # merged lesson cell — must not be absorbed as a lesson of whichever
    # day was current (real-fixture regression: schedule_sample.pdf's
    # footer row has this exact shape and was wrongly absorbed into
    # Суббота before this was fixed).
    rows.append(day_row(None, [Cell("Начальник отдела", 0, 500), None, None], day_marker=True, y0=0, y1=10))
    schedule = build_schedule(rows, GROUP_COL, TABLE_X1)
    assert set(schedule.keys()) == {
        "Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"
    }
    saturday_lessons = schedule["Суббота"].lessons
    assert all("Начальник отдела" not in (l.upper or "") and "Начальник отдела" not in (l.lower or "") for l in saturday_lessons)
    assert schedule["Суббота"].y1 == 10  # footer row's bbox must not extend Суббота's bounds


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


def test_header_zone_excludes_first_day_row_that_is_also_a_lesson_row():
    """Real-fixture shape (schedule_sample.pdf rows 0-2): the row carrying
    the FIRST day's label also carries that day's first lesson, so it has
    day_marker=True. The header zone must still end BEFORE it, otherwise a
    group name mentioned in that row's lesson text would be matched as a
    header (design spec 5.1)."""
    rows = [
        day_row(None, [Cell("СОГЛАСОВАНО", 0, 30), None, None, None], day_marker=True, y0=0, y1=10),
        day_row("Часы", [Cell("Дни", 0, 30), Cell("Часы", 30, 60), Cell("Б-Эв-101", 60, 300), Cell("Б-Мв-101", 300, 500)], day_marker=True, y0=10, y1=20),
        # Monday's label AND Monday's first lesson in one row -> day_marker=True
        day_row("8.30-10.00", [Cell("Понедельник", 0, 30), Cell("8.30-10.00", 30, 60), Cell("совм. с Б-А-301", 60, 300), Cell("Матан", 300, 500)], day_marker=True, y0=20, y1=30),
    ]
    # Real header rows still resolve normally.
    assert find_header_column_index(rows, "Б-Мв-101") == (1, 3)
    # "Б-А-301" exists ONLY in the lesson text of that first day row, which
    # has day_marker=True — the old `first row with day_marker is False`
    # boundary kept it inside the header zone and happily returned (2, 2).
    with pytest.raises(ValueError):
        find_header_column_index(rows, "Б-А-301")


def test_header_zone_end_index_stops_at_first_time_range_row():
    from schedule_bot.pdf_parser import _header_zone_end_index

    rows = [
        day_row(None, [Cell("hdr", 0, 30)], day_marker=True),
        day_row("Часы", [Cell("Дни", 0, 30)], day_marker=True),
        day_row("8.30-10.00", [Cell("Пн", 0, 30)], day_marker=True),
    ]
    assert _header_zone_end_index(rows) == 2


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
        day_row("12.00-13.30", [None, Cell("12.00-13.30", 30, 60), None, None], y0=0, y1=10),
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


def test_build_schedule_ignores_a_sub_header_row_before_the_first_real_day():
    # A day_marker row whose cells[0] text is NOT an actual day name (a
    # stray sub-header like "Часы/1 подгр/2 подгр" that also happens to
    # have a non-None cells[0]) must NOT consume a slot from DAY_ORDER.
    rows = [
        day_row(None, [Cell("Часы", 0, 10), Cell("1 подгр", 10, 20), Cell("2 подгр", 20, 30)], day_marker=True, y0=0, y1=5),
        day_row("8.30-10.00", [Cell("Понедельник", 0, 10), Cell("8.30-10.00", 10, 20), Cell("Лекция", 20, 30)], day_marker=True, y0=5, y1=15),
        day_row("8.30-10.00", [Cell("Вторник", 0, 10), Cell("8.30-10.00", 10, 20), Cell("Семинар", 20, 30)], day_marker=True, y0=15, y1=25),
    ]
    result = build_schedule(rows, group_column_index=2, table_x1=30.0, time_column_index=1)
    assert "Понедельник" in result
    assert result["Понедельник"].lessons[0].upper == "Лекция"
    assert "Вторник" in result
    assert result["Вторник"].lessons[0].upper == "Семинар"


def test_build_schedule_does_not_misassign_an_ambiguous_single_letter_day_prefix():
    # "С" is a prefix of both "Среда" and "Суббота" — a day_marker row
    # whose (reversed-line-normalized) text is this short must not be
    # silently assigned to whichever day happens first in DAY_ORDER.
    rows = [
        day_row(None, [Cell("С", 0, 30), None, None], day_marker=True, y0=0, y1=10),
        day_row("8.30-10.00", [None, Cell("8.30-10.00", 30, 60), Cell("Матан", 300, 500)], y0=0, y1=10),
    ]
    schedule = build_schedule(rows, GROUP_COL, TABLE_X1)
    assert "Среда" not in schedule
    assert "Суббота" not in schedule

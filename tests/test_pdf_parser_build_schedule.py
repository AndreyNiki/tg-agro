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

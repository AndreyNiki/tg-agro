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

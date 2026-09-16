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

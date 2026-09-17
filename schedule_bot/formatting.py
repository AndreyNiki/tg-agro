from datetime import date

from schedule_bot.pdf_parser import Schedule
from schedule_bot.weektype import WEEK_LOWER, WEEK_UPPER

# Accusative form for "проводится в ... неделю" — WEEK_UPPER/WEEK_LOWER are
# nominative ("Верхняя"/"Нижняя"), which doesn't decline correctly after "в".
_WEEK_ACCUSATIVE = {WEEK_UPPER: "Верхнюю", WEEK_LOWER: "Нижнюю"}


def format_day_schedule(day: str, on_date: date, week_type: str, schedule: Schedule) -> str:
    header = f"📅 {day}, {on_date.strftime('%d.%m.%Y')} ({week_type} неделя)"
    day_schedule = schedule.days.get(day)
    if day_schedule is None or not day_schedule.lessons:
        return f"{header}\n\nЗанятий нет."

    lines = [header]
    for lesson in day_schedule.lessons:
        is_upper_week = week_type == WEEK_UPPER
        text = lesson.upper if is_upper_week else lesson.lower
        other_text = lesson.lower if is_upper_week else lesson.upper
        if text is None:
            if other_text is not None:
                other_week = WEEK_LOWER if is_upper_week else WEEK_UPPER
                text = f"нет пары ({other_text}, {_WEEK_ACCUSATIVE[other_week]} неделю)"
            else:
                text = "нет пары"
        lines.append("")
        lines.append(f"🕐 {lesson.time} — 📝 {text}")
    return "\n".join(lines)

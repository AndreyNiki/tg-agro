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

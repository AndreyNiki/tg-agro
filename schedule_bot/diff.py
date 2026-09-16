from schedule_bot.pdf_parser import DAY_ORDER, Schedule


def _describe(pair: tuple[str | None, str | None] | None) -> str:
    if pair is None:
        return "нет пары"
    upper, lower = pair
    if upper == lower:
        return upper or "нет пары"
    return f"верхняя: {upper or 'нет пары'}; нижняя: {lower or 'нет пары'}"


def compute_diff(old: Schedule | None, new: Schedule) -> list[str]:
    if old is None:
        return []

    changes: list[str] = []
    old_days = old.days

    for day in DAY_ORDER:
        old_day = old_days.get(day)
        old_lessons = {
            lesson.time: (lesson.upper, lesson.lower) for lesson in (old_day.lessons if old_day else [])
        }
        new_day = new.days.get(day)
        new_lessons = {
            lesson.time: (lesson.upper, lesson.lower) for lesson in (new_day.lessons if new_day else [])
        }
        all_times = sorted(set(old_lessons) | set(new_lessons))
        for time_label in all_times:
            old_pair = old_lessons.get(time_label)
            new_pair = new_lessons.get(time_label)
            if old_pair != new_pair:
                changes.append(
                    f"{day} {time_label}: {_describe(old_pair)} -> {_describe(new_pair)}"
                )
    return changes

from datetime import datetime, timedelta

MIN_GROUP_SIZE_FOR_ELECTION = 3
STAROSTA_CHANGE_COOLDOWN_DAYS = 14


def has_enough_members(member_count: int) -> bool:
    return member_count >= MIN_GROUP_SIZE_FOR_ELECTION


def cooldown_remaining(last_change_at: str | None, now: datetime) -> timedelta | None:
    """Remaining cooldown before a new change-vote can start, or None if
    one can start right now (never changed before, or 14+ days have
    passed since last_change_at)."""
    if not last_change_at:
        return None
    last_change = datetime.fromisoformat(last_change_at)
    remaining = timedelta(days=STAROSTA_CHANGE_COOLDOWN_DAYS) - (now - last_change)
    return remaining if remaining > timedelta(0) else None


def format_candidate_label(member) -> str:
    if member.display_name and member.telegram_username:
        return f"{member.display_name} (@{member.telegram_username})"
    if member.display_name:
        return member.display_name
    if member.telegram_username:
        return f"@{member.telegram_username}"
    return "Неизвестный"


def format_starosta_welcome(group_name: str, assigned_by: str) -> str:
    reason = "по результатам голосования группы" if assigned_by == "election" else "решением администратора"
    return (
        f"🎉 Вы назначены старостой группы «{group_name}» ({reason}).\n\n"
        "Доступные вам команды:\n"
        "/starosta — статус и управление\n"
        "/votes — открытые голосования группы\n"
        "(дополнительные привилегии старосты появятся позже)"
    )

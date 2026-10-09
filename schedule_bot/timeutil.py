from datetime import date, datetime
from zoneinfo import ZoneInfo


def today_in_configured_timezone(tz_name: str | None) -> date:
    """"Today" for daily-send/day-picker purposes must follow the
    group's configured timezone, not the server's local date — otherwise
    a container running in UTC computes the wrong day (and week type)
    around midnight for any non-UTC timezone."""
    if not tz_name:
        return date.today()
    return datetime.now(ZoneInfo(tz_name)).date()

from datetime import date, datetime
from zoneinfo import ZoneInfo

from schedule_bot import storage


def today_in_configured_timezone(conn) -> date:
    """"Today" for daily-send/day-picker purposes must follow the
    TIMEZONE setting, not the server's local date — otherwise a
    container running in UTC computes the wrong day (and week type)
    around midnight for any non-UTC TIMEZONE."""
    tz_name = storage.get_setting(conn, "timezone")
    if not tz_name:
        return date.today()
    return datetime.now(ZoneInfo(tz_name)).date()

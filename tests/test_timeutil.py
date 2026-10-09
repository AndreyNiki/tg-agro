from datetime import date

import pytest

from schedule_bot.timeutil import today_in_configured_timezone


def test_returns_system_today_when_no_timezone_setting():
    assert today_in_configured_timezone(None) == date.today()


def test_uses_configured_timezone_not_system_local(monkeypatch):
    """A UTC-hosted process with tz_name=Europe/Moscow (UTC+3) must report
    tomorrow's date once it's past 21:00 UTC, even though date.today() in
    the system's own (UTC) timezone still says today."""
    import schedule_bot.timeutil as timeutil_module
    from datetime import datetime
    from zoneinfo import ZoneInfo

    fixed_utc = datetime(2026, 9, 17, 22, 0, tzinfo=ZoneInfo("UTC"))

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_utc.astimezone(tz) if tz else fixed_utc

    monkeypatch.setattr(timeutil_module, "datetime", FixedDatetime)

    assert today_in_configured_timezone("Europe/Moscow") == date(2026, 9, 18)

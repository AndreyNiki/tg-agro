import sqlite3
from unittest.mock import MagicMock

import pytest

from schedule_bot import storage
from schedule_bot.config import Config
from schedule_bot.weektype import WeekFormula
from main import build_application


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    storage.init_db(connection)
    return connection


@pytest.fixture
def config():
    return Config(
        telegram_token="test-token",
        admin_chat_id=999,
        db_path=":memory:",
        pdf_cache_dir="/tmp/cache",
        default_pdf_source="/tmp/does-not-matter.pdf",
        default_group_name="Б-Мв-101",
        default_weektype_url="https://example.com/week",
        default_daily_send_time="09:00",
        default_check_interval_minutes=60,
        default_timezone="Europe/Moscow",
    )


def test_build_application_registers_all_expected_handlers(conn, config, monkeypatch):
    monkeypatch.setattr(
        "main.fetch_week_formula",
        lambda url: WeekFormula(base_month=1, base_day=5, even_index_is_upper=True),
    )

    application = build_application(config, conn)

    command_names = set()
    for handlers in application.handlers.values():
        for handler in handlers:
            if hasattr(handler, "commands"):
                command_names.update(handler.commands)

    assert {
        "start",
        "stop",
        "schedule",
        "set_pdf_source",
        "set_group",
        "set_send_time",
        "set_check_interval",
        "refresh",
        "alert",
    }.issubset(command_names)


def test_build_application_seeds_default_settings_from_config(conn, config, monkeypatch):
    monkeypatch.setattr(
        "main.fetch_week_formula",
        lambda url: WeekFormula(base_month=1, base_day=5, even_index_is_upper=True),
    )

    build_application(config, conn)

    assert storage.get_setting(conn, "pdf_source") == "/tmp/does-not-matter.pdf"
    assert storage.get_setting(conn, "group_name") == "Б-Мв-101"
    assert storage.get_setting(conn, "daily_send_time") == "09:00"


def test_build_application_week_formula_reflects_later_holder_updates(conn, config, monkeypatch):
    from datetime import date

    monkeypatch.setattr(
        "main.fetch_week_formula",
        lambda url: WeekFormula(base_month=1, base_day=5, even_index_is_upper=True),
    )

    application = build_application(config, conn)
    week_formula = application.bot_data["week_formula"]
    on_date = date(2026, 9, 16)
    first_result = week_formula.compute(on_date)

    # Simulate what scheduler.weekly_weektype_refresh_job does: mutate the
    # holder's formula field in place with a flipped parity rule.
    holder = application.bot_data["_week_formula_holder"]
    holder.formula = WeekFormula(base_month=1, base_day=5, even_index_is_upper=False)

    second_result = week_formula.compute(on_date)
    assert first_result != second_result

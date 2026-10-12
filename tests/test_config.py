import pytest
from schedule_bot.config import load_config


def _base_env():
    return {
        "TELEGRAM_BOT_TOKEN": "abc123",
        "ADMIN_CHAT_ID": "42",
    }


def test_load_config_reads_required_and_defaults():
    env = _base_env()
    config = load_config(env)
    assert config.telegram_token == "abc123"
    assert config.admin_chat_id == 42
    assert config.db_path == "./data/schedule.db"
    assert config.pdf_cache_dir == "./data/pdf_cache"
    assert config.default_weektype_url == "https://www.vavilovsar.ru/ucheba/raspisanie-zanyatii"
    assert config.default_daily_send_time == "09:00"
    assert config.default_check_interval_minutes == 60
    assert config.default_timezone == "Europe/Moscow"


def test_load_config_raises_on_missing_required_var():
    with pytest.raises(ValueError):
        load_config({"ADMIN_CHAT_ID": "42"})


def test_load_config_defaults_catalog_refresh_interval_to_one_week():
    config = load_config(_base_env())
    assert config.default_catalog_refresh_interval_minutes == 10080

import pytest
from schedule_bot.config import load_config


def _base_env():
    return {
        "TELEGRAM_BOT_TOKEN": "abc123",
        "ADMIN_CHAT_ID": "42",
        "PDF_SOURCE": "https://example.com/schedule.pdf",
    }


def test_load_config_reads_required_and_defaults():
    env = _base_env()
    config = load_config(env)
    assert config.telegram_token == "abc123"
    assert config.admin_chat_id == 42
    assert config.default_pdf_source == "https://example.com/schedule.pdf"
    assert config.db_path == "./data/schedule.db"
    assert config.pdf_cache_dir == "./data/pdf_cache"
    assert config.default_group_name == "Б-Мв-101"
    assert config.default_weektype_url == "https://www.vavilovsar.ru/ucheba/raspisanie-zanyatii"
    assert config.default_daily_send_time == "09:00"
    assert config.default_check_interval_minutes == 60
    assert config.default_timezone == "Europe/Moscow"


def test_load_config_raises_on_missing_required_var():
    with pytest.raises(ValueError):
        load_config({"ADMIN_CHAT_ID": "42", "PDF_SOURCE": "x"})


def test_load_config_reads_legacy_group_source_page_url_when_present():
    env = _base_env()
    env["LEGACY_GROUP_SOURCE_PAGE_URL"] = "https://example.com/schedules"
    config = load_config(env)
    assert config.legacy_group_source_page_url == "https://example.com/schedules"


def test_load_config_defaults_legacy_group_source_page_url_to_none():
    config = load_config(_base_env())
    assert config.legacy_group_source_page_url is None

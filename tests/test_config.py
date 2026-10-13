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


def test_load_config_legacy_migration_is_none_when_vars_absent():
    config = load_config(_base_env())
    assert config.legacy_migration_catalog_url is None
    assert config.legacy_migration_institute_name is None
    assert config.legacy_migration_group_name is None
    assert config.legacy_migration is None


def test_load_config_legacy_migration_is_none_when_only_some_vars_present():
    env = _base_env()
    env["LEGACY_MIGRATION_INSTITUT"] = "https://example.com/institut"
    env["LEGACY_MIGRATION_INSTITUT_NAME"] = "Институт агробизнеса"
    # LEGACY_MIGRATION_GROUP_NAME intentionally missing
    config = load_config(env)
    assert config.legacy_migration is None


def test_load_config_legacy_migration_set_when_all_three_vars_present():
    env = _base_env()
    env["LEGACY_MIGRATION_INSTITUT"] = "https://example.com/institut"
    env["LEGACY_MIGRATION_INSTITUT_NAME"] = "Институт агробизнеса"
    env["LEGACY_MIGRATION_GROUP_NAME"] = "Б-Мв-101"
    config = load_config(env)
    assert config.legacy_migration == (
        "https://example.com/institut", "Институт агробизнеса", "Б-Мв-101",
    )


def test_load_config_legacy_migration_is_none_when_a_var_is_whitespace_only():
    env = _base_env()
    env["LEGACY_MIGRATION_INSTITUT"] = "   "
    env["LEGACY_MIGRATION_INSTITUT_NAME"] = "Институт агробизнеса"
    env["LEGACY_MIGRATION_GROUP_NAME"] = "Б-Мв-101"
    config = load_config(env)
    assert config.legacy_migration_catalog_url is None
    assert config.legacy_migration is None


def test_load_config_legacy_migration_strips_surrounding_whitespace():
    env = _base_env()
    env["LEGACY_MIGRATION_INSTITUT"] = "  https://example.com/institut  "
    env["LEGACY_MIGRATION_INSTITUT_NAME"] = " Институт агробизнеса "
    env["LEGACY_MIGRATION_GROUP_NAME"] = " Б-Мв-101 "
    config = load_config(env)
    assert config.legacy_migration == (
        "https://example.com/institut", "Институт агробизнеса", "Б-Мв-101",
    )

from dataclasses import dataclass
from typing import Mapping


@dataclass
class Config:
    telegram_token: str
    admin_chat_id: int
    db_path: str
    pdf_cache_dir: str
    default_weektype_url: str
    default_daily_send_time: str
    default_check_interval_minutes: int
    default_timezone: str
    default_catalog_refresh_interval_minutes: int
    legacy_migration_catalog_url: str | None = None
    legacy_migration_institute_name: str | None = None
    legacy_migration_group_name: str | None = None

    @property
    def legacy_migration(self) -> tuple[str, str, str] | None:
        if self.legacy_migration_catalog_url and self.legacy_migration_institute_name and self.legacy_migration_group_name:
            return (
                self.legacy_migration_catalog_url,
                self.legacy_migration_institute_name,
                self.legacy_migration_group_name,
            )
        return None


def load_config(env: Mapping[str, str]) -> Config:
    def require(key: str) -> str:
        value = env.get(key)
        if not value:
            raise ValueError(f"Missing required env var: {key}")
        return value

    return Config(
        telegram_token=require("TELEGRAM_BOT_TOKEN"),
        admin_chat_id=int(require("ADMIN_CHAT_ID")),
        pdf_cache_dir=env.get("PDF_CACHE_DIR", "./data/pdf_cache"),
        db_path=env.get("DB_PATH", "./data/schedule.db"),
        default_weektype_url=env.get(
            "WEEKTYPE_URL", "https://www.vavilovsar.ru/ucheba/raspisanie-zanyatii"
        ),
        default_daily_send_time=env.get("DAILY_SEND_TIME", "09:00"),
        default_check_interval_minutes=int(env.get("CHECK_INTERVAL_MINUTES", "60")),
        default_timezone=env.get("TIMEZONE", "Europe/Moscow"),
        default_catalog_refresh_interval_minutes=int(
            env.get("CATALOG_REFRESH_INTERVAL_MINUTES", "10080")  # 1 week
        ),
        legacy_migration_catalog_url=env.get("LEGACY_MIGRATION_INSTITUT") or None,
        legacy_migration_institute_name=env.get("LEGACY_MIGRATION_INSTITUT_NAME") or None,
        legacy_migration_group_name=env.get("LEGACY_MIGRATION_GROUP_NAME") or None,
    )

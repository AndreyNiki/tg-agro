from dataclasses import dataclass
from typing import Mapping


@dataclass
class Config:
    telegram_token: str
    admin_chat_id: int
    db_path: str
    pdf_cache_dir: str
    default_pdf_source: str
    default_group_name: str
    default_weektype_url: str
    default_daily_send_time: str
    default_check_interval_minutes: int
    default_timezone: str


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
        default_pdf_source=require("PDF_SOURCE"),
        default_group_name=env.get("GROUP_NAME", "Б-Мв-101"),
        default_weektype_url=env.get(
            "WEEKTYPE_URL", "https://www.vavilovsar.ru/ucheba/raspisanie-zanyatii"
        ),
        default_daily_send_time=env.get("DAILY_SEND_TIME", "09:00"),
        default_check_interval_minutes=int(env.get("CHECK_INTERVAL_MINUTES", "60")),
        default_timezone=env.get("TIMEZONE", "Europe/Moscow"),
    )

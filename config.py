"""Конфигурация бота: все параметры берутся из переменных окружения / .env."""
from __future__ import annotations

from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Токен бота от @BotFather
    bot_token: SecretStr

    # Единственный пользователь, которому разрешено пользоваться ботом.
    # Поддерживаются оба имени переменной: ALLOWED_USER_ID и TELEGRAM_USER_ID.
    allowed_user_id: int = Field(
        validation_alias=AliasChoices("ALLOWED_USER_ID", "TELEGRAM_USER_ID"),
    )

    # DSN PostgreSQL, например: postgresql://user:password@postgres:5432/filestorage
    db_url: str

    # Базовый URL локального Telegram Bot API Server (без /bot{token}/{method})
    local_server_url: str = "http://telegram-bot-api:8081"

    # Корневая директория хранилища
    storage_dir: Path = Path("/storage")

    # Часовой пояс для имён папок YYYY-MM и отображения дат
    timezone: str = "UTC"

    @field_validator("db_url")
    @classmethod
    def _normalize_db_url(cls, value: str) -> str:
        # asyncpg не понимает префикс SQLAlchemy "postgresql+asyncpg://"
        return value.strip().replace("postgresql+asyncpg://", "postgresql://", 1)

    @field_validator("local_server_url")
    @classmethod
    def _normalize_server_url(cls, value: str) -> str:
        value = value.strip()
        # Если передан полный шаблон ".../bot{token}/{method}" — отрезаем хвост
        marker = value.find("/bot{")
        if marker != -1:
            value = value[:marker]
        return value.rstrip("/")

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        ZoneInfo(value)  # выбросит ошибку на неизвестной таймзоне
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

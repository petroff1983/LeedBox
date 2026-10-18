"""Validated runtime settings; no credentials are logged."""

import re
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from leedbox.domain.materials import http_url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LEEDBOX_",
        extra="ignore",
        env_ignore_empty=True,
        hide_input_in_errors=True,
        populate_by_name=True,
    )

    environment: Literal["development", "test", "production"] = "development"
    database_url: SecretStr
    bot_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias="BOT_TOKEN",
    )
    owner_ids: frozenset[int] = frozenset()
    web_host: str = "127.0.0.1"
    web_port: int = Field(default=5000, ge=1, le=65535)
    public_base_url: str | None = None
    owner_media_directory: Path = Path("var/owner-media")
    clck_api_key: SecretStr = SecretStr("")
    youtube_api_key: SecretStr = SecretStr("")
    zenstat_api_key: SecretStr = SecretStr("")
    apify_api_token: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices("LEEDBOX_APIFY_API_TOKEN", "APIFY_API_TOKEN"),
    )
    deepseek_api_key: SecretStr = Field(
        default=SecretStr(""),
        validation_alias=AliasChoices(
            "LEEDBOX_DEEPSEEK_API_KEY", "LEEDBOX_DEEPSEEK_API", "deepsee_API"
        ),
    )
    youtube_daily_units: int = Field(default=9000, ge=1, le=10000)
    youtube_daily_scans: int = Field(default=3, ge=1, le=100)
    youtube_scan_limit: int = Field(default=500, ge=50, le=1000)
    ai_workers: int = Field(default=4, ge=1, le=8)
    admin_enabled: bool = False
    admin_path: str = "/owner"
    web_secret_key: SecretStr = SecretStr("")
    admin_session_seconds: int = Field(default=3600, ge=300, le=86400)
    trust_proxy: bool = False
    robokassa_enabled: bool = False
    robokassa_test: bool = True
    robokassa_merchant: str = ""
    robokassa_password1: SecretStr = SecretStr("")
    robokassa_password2: SecretStr = SecretStr("")
    robokassa_hash: Literal["md5", "sha256"] = "md5"
    stars_enabled: bool = False
    payment_operations_enabled: bool = False
    robokassa_password3: SecretStr = SecretStr("")
    telegram_test_api: bool = False
    telegram_system_proxy: bool = False

    @model_validator(mode="after")
    def stars_configuration(self) -> "Settings":
        if self.telegram_test_api and self.environment == "production":
            raise ValueError("Telegram test API cannot be used in production")
        if self.stars_enabled:
            if not re.fullmatch(
                r"[1-9][0-9]{0,18}:[A-Za-z0-9_-]{20,}", self.bot_token.get_secret_value()
            ):
                raise ValueError("Stars requires a valid BOT_TOKEN")
            if self.environment != "production" and not self.telegram_test_api:
                raise ValueError("Local Stars purchases require the dedicated Telegram test API")
        return self

    @model_validator(mode="after")
    def robokassa_configuration(self) -> "Settings":
        if self.robokassa_enabled:
            if not re.fullmatch(
                r"[1-9][0-9]{0,18}:[A-Za-z0-9_-]{20,}", self.bot_token.get_secret_value()
            ):
                raise ValueError("Robokassa requires a valid BOT_TOKEN for order isolation")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", self.robokassa_merchant):
                raise ValueError("Robokassa requires merchant login")
            passwords = (
                self.robokassa_password1.get_secret_value(),
                self.robokassa_password2.get_secret_value(),
            )
            if any(not password or ":" in password for password in passwords):
                raise ValueError("Robokassa requires private passwords for the selected mode")
            if not self.robokassa_test and self.environment != "production":
                raise ValueError("Live Robokassa payments require production configuration")
        return self

    @field_validator("admin_path")
    @classmethod
    def admin_url(cls, value: str) -> str:
        if not re.fullmatch(r"/[a-z][a-z0-9_-]{2,50}", value) or value in {
            "/health",
            "/ready",
            "/static",
            "/cabinet",
        }:
            raise ValueError("Admin path must be a separate single URL segment")
        return value

    @model_validator(mode="after")
    def admin_configuration(self) -> "Settings":
        if self.admin_enabled:
            secret = self.web_secret_key.get_secret_value()
            if not self.owner_ids:
                raise ValueError("Admin requires a nonempty owner allowlist")
            if (
                len(secret) < 32
                or len(set(secret)) < 12
                or "replace" in secret.lower()
                or secret == self.bot_token.get_secret_value()
            ):
                raise ValueError(
                    "Admin requires a separate strong web secret (at least 32 characters)"
                )
            if self.environment == "production" and (
                not self.public_base_url or urlsplit(self.public_base_url).scheme != "https"
            ):
                raise ValueError("Production admin requires an HTTPS public base URL")
        return self

    @field_validator("public_base_url")
    @classmethod
    def public_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        clean = http_url(value)
        parsed = urlsplit(clean)
        if parsed.query or parsed.fragment:
            raise ValueError("Public base URL must not contain query or fragment")
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("Public base URL requires HTTPS; HTTP is allowed only for localhost")
        return clean.rstrip("/")

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr) -> SecretStr:
        url = make_url(value.get_secret_value())
        if url.drivername != "postgresql+psycopg" or not url.database:
            raise ValueError("A postgresql+psycopg database URL is required")
        return value

    @field_validator("owner_ids")
    @classmethod
    def positive_ids(cls, values: frozenset[int]) -> frozenset[int]:
        if any(value <= 0 for value in values):
            raise ValueError("Owner Telegram IDs must be positive")
        return values

    def is_owner(self, telegram_id: int) -> bool:
        return telegram_id in self.owner_ids

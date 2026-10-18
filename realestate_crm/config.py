import ipaddress
import logging
import re
from functools import lru_cache
from typing import Annotated
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    telegram_bot_token: SecretStr
    telegram_webhook_secret: SecretStr
    telegram_webhook_url: str
    authorized_telegram_user_ids: Annotated[list[int], NoDecode] = Field(
        min_length=1,
        validation_alias=AliasChoices(
            "AUTHORIZED_TELEGRAM_USER_ID",
            "authorized_telegram_user_ids",
            "authorized_telegram_user_id",
        ),
    )
    supabase_url: str
    supabase_service_role_key: SecretStr
    supabase_database_url: SecretStr
    app_timezone: str = "Africa/Addis_Ababa"
    max_import_file_mb: int = Field(default=20, ge=1, le=20)
    max_import_rows: int = Field(default=100_000, ge=1, le=1_000_000)
    import_batch_size: int = Field(default=500, ge=50, le=5000)
    import_retention_hours: int = Field(default=24, ge=1, le=720)
    max_call_attempts: int = Field(default=8, ge=1, le=100)
    default_daily_call_target: int = Field(default=50, ge=1, le=5000)
    follow_up_cron_secret: SecretStr
    max_search_results: int = Field(default=10, ge=1, le=50)
    export_max_rows: int = Field(default=50_000, ge=1, le=100_000)
    ai_enabled: bool = False
    ai_configuration_error: str | None = Field(default=None, exclude=True)
    ai_provider: str = "openai_compatible"
    ai_base_url: str | None = None
    ai_api_key: SecretStr | None = None
    ai_model: str | None = None
    ai_request_timeout_seconds: int = Field(default=25, ge=3, le=60)
    ai_max_input_chars: int = Field(default=5000, ge=500, le=20000)
    ai_max_output_chars: int = Field(default=4000, ge=500, le=12000)
    ai_daily_request_limit: int = Field(default=20, ge=1, le=500)
    match_allow_near_budget: bool = False

    match_budget_weight: int = Field(default=35, ge=0, le=100)
    match_location_weight: int = Field(default=25, ge=0, le=100)
    match_property_type_weight: int = Field(default=15, ge=0, le=100)
    match_bedrooms_weight: int = Field(default=10, ge=0, le=100)
    match_payment_weight: int = Field(default=10, ge=0, le=100)
    match_timeline_weight: int = Field(default=5, ge=0, le=100)
    lead_interest_weight: int = Field(default=25, ge=0, le=100)
    lead_requirements_budget_weight: int = Field(default=20, ge=0, le=100)
    lead_timeline_weight: int = Field(default=20, ge=0, le=100)
    lead_engagement_weight: int = Field(default=15, ge=0, le=100)
    lead_next_action_weight: int = Field(default=10, ge=0, le=100)
    lead_recent_positive_weight: int = Field(default=10, ge=0, le=100)
    queue_priority_order: str = (
        "overdue_high,due_followup,high_qualified,qualified,interested,new,retry"
    )

    @field_validator("authorized_telegram_user_ids", mode="before")
    @classmethod
    def parse_authorized_telegram_user_ids(cls, value: object) -> list[int]:
        if isinstance(value, str):
            parts = value.split(",")
        elif isinstance(value, (list, tuple, set)):
            parts = list(value)
        else:
            parts = [value]

        if not parts or any(not str(part).strip() for part in parts):
            raise ValueError("AUTHORIZED_TELEGRAM_USER_ID must contain one or more numeric IDs")
        try:
            user_ids = [int(str(part).strip()) for part in parts]
        except (TypeError, ValueError) as exc:
            raise ValueError("AUTHORIZED_TELEGRAM_USER_ID must contain numeric IDs") from exc
        if any(user_id < 1 for user_id in user_ids):
            raise ValueError("AUTHORIZED_TELEGRAM_USER_ID values must be positive")
        return list(dict.fromkeys(user_ids))

    @property
    def authorized_telegram_user_id(self) -> int:
        """Return the first configured administrator for legacy callers."""
        return self.authorized_telegram_user_ids[0]

    @model_validator(mode="after")
    def validate_ai_settings(self):
        if self.ai_enabled:
            problems = []
            if self.ai_provider != "openai_compatible":
                problems.append("AI_PROVIDER must be openai_compatible for this adapter")
            if not self.ai_base_url or not self.ai_api_key or not self.ai_model:
                problems.append("AI requires AI_BASE_URL, AI_API_KEY, and AI_MODEL")
            if self.ai_base_url and not self.ai_base_url.startswith("https://"):
                problems.append("AI_BASE_URL must use HTTPS")
            if problems:
                self.ai_enabled = False
                self.ai_configuration_error = "; ".join(problems)
        return self

    @field_validator("telegram_bot_token")
    @classmethod
    def valid_bot_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value()
        if not re.fullmatch(r"[0-9]{5,}:[A-Za-z0-9_-]{20,}", token):
            raise ValueError("TELEGRAM_BOT_TOKEN must use the Telegram bot-token format")
        return value

    @model_validator(mode="after")
    def validate_scoring_and_queue(self):
        match_total = sum(
            getattr(self, f"match_{key}_weight")
            for key in ("budget", "location", "property_type", "bedrooms", "payment", "timeline")
        )
        lead_total = sum(
            getattr(self, f"lead_{key}_weight")
            for key in (
                "interest",
                "requirements_budget",
                "timeline",
                "engagement",
                "next_action",
                "recent_positive",
            )
        )
        if match_total != 100:
            raise ValueError("MATCH_*_WEIGHT values must sum to 100")
        if lead_total != 100:
            raise ValueError("LEAD_*_WEIGHT values must sum to 100")
        queue = [item.strip() for item in self.queue_priority_order.split(",") if item.strip()]
        allowed = {
            "overdue_high",
            "due_followup",
            "high_qualified",
            "qualified",
            "interested",
            "new",
            "retry",
        }
        if len(queue) != len(allowed) or set(queue) != allowed:
            raise ValueError("QUEUE_PRIORITY_ORDER must list every supported queue group once")
        return self

    @field_validator("app_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("APP_TIMEZONE must be a valid IANA timezone") from exc
        return value

    @field_validator("telegram_webhook_secret")
    @classmethod
    def valid_webhook_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if (
            not 32 <= len(secret) <= 256
            or len(set(secret)) < 10
            or not re.fullmatch(r"[A-Za-z0-9_-]+", secret)
        ):
            raise ValueError("TELEGRAM_WEBHOOK_SECRET must be 32–256 varied URL-safe characters")
        return value

    @field_validator("follow_up_cron_secret")
    @classmethod
    def valid_cron_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if (
            not 32 <= len(secret) <= 256
            or len(set(secret)) < 10
            or not secret.isascii()
            or any(character.isspace() for character in secret)
        ):
            raise ValueError("FOLLOW_UP_CRON_SECRET must be 32–256 varied ASCII characters")
        return value

    @field_validator("telegram_webhook_url")
    @classmethod
    def valid_webhook_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.fragment
        ):
            raise ValueError("TELEGRAM_WEBHOOK_URL must be a public HTTPS URL without credentials")
        return value

    @field_validator("supabase_url")
    @classmethod
    def valid_supabase_url(cls, value: str) -> str:
        parts = urlsplit(value)
        is_loopback = (
            parts.hostname in {"localhost", "127.0.0.1", "::1"} if parts.hostname else False
        )
        if parts.hostname:
            try:
                is_loopback = is_loopback or ipaddress.ip_address(parts.hostname).is_loopback
            except ValueError:
                pass
        if (
            (parts.scheme != "https" and not (parts.scheme == "http" and is_loopback))
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError("SUPABASE_URL must be an HTTP(S) URL without credentials or query")
        return value.rstrip("/")

    @field_validator("supabase_database_url")
    @classmethod
    def valid_database_url(cls, value: SecretStr) -> SecretStr:
        raw_url = value.get_secret_value()
        parts = urlsplit(raw_url)
        if parts.scheme not in {"postgres", "postgresql"} or not parts.hostname:
            raise ValueError("SUPABASE_DATABASE_URL must be a PostgreSQL connection URL")
        is_loopback = parts.hostname in {"localhost", "127.0.0.1", "::1"}
        try:
            is_loopback = is_loopback or ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            pass
        sslmode = parse_qs(parts.query).get("sslmode", [None])[0]
        if not is_loopback and sslmode not in {"require", "verify-ca", "verify-full"}:
            raise ValueError("SUPABASE_DATABASE_URL must require SSL for remote databases")
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    if settings.ai_configuration_error:
        logger.warning("Optional AI disabled: %s", settings.ai_configuration_error)
    return settings

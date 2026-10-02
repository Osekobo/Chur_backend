"""Application configuration loaded from environment variables / .env file."""

from __future__ import annotations

from functools import lru_cache
from typing import Annotated, Literal

from pydantic import Field, PostgresDsn, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings.

    All values are read from environment variables, falling back to a local
    ``.env`` file. Field names map to upper-cased environment variables, e.g.
    ``SECRET_KEY`` or ``DATABASE_URL``.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Application ---------------------------------------------------
    PROJECT_NAME: str = "Church Finance System API"
    VERSION: str = "1.0.0"
    ENVIRONMENT: Literal["development", "staging", "production", "test"] = "development"
    DEBUG: bool = True
    API_V1_PREFIX: str = "/api/v1"

    # --- Database ------------------------------------------------------
    # Async DSN used by the application, e.g.
    # postgresql+asyncpg://user:pass@localhost:5432/church_finance
    DATABASE_URL: PostgresDsn
    # Optional sync DSN used by Alembic and management scripts. When omitted it
    # is derived from DATABASE_URL by swapping the async driver for psycopg2.
    DATABASE_SYNC_URL: str | None = None

    # --- Security ------------------------------------------------------
    SECRET_KEY: str = Field(min_length=32)
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60
    REFRESH_TOKEN_EXPIRE_DAYS: int = 14
    PASSWORD_RESET_EXPIRE_MINUTES: int = 30
    # When true the password reset link is returned in the API response and
    # written to the application log. Intended for local development only.
    RETURN_RESET_LINK_IN_RESPONSE: bool = True

    # --- Registration --------------------------------------------------
    # The shared ledger is visible to every authenticated church user. Disable
    # public sign-up and provision accounts from the database instead. Roles
    # (is_superuser, is_active) are always managed in PostgreSQL, never here.
    ALLOW_PUBLIC_REGISTRATION: bool = True

    # --- CORS / frontend ----------------------------------------------
    FRONTEND_URL: str = "http://localhost:5173"
    # NoDecode lets the validator below accept a comma separated string.
    CORS_ORIGINS: Annotated[list[str], NoDecode] = [
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ]

    # --- Outbound email (password reset) -------------------------------
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD: str | None = None
    SMTP_USE_TLS: bool = True
    SMTP_FROM_EMAIL: str = "no-reply@church.local"
    SMTP_FROM_NAME: str = "Church Finance System"

    # --- Misc ----------------------------------------------------------
    DEFAULT_PAGE_SIZE: int = 50
    MAX_PAGE_SIZE: int = 500
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def _force_async_driver(cls, value: object) -> object:
        """Upgrade a driverless DSN to the async driver the app runs on.

        Managed Postgres providers hand out ``postgresql://user:pass@host/db``
        URLs, which SQLAlchemy would route through psycopg2 and break the async
        engine on. Patching the driver here keeps such URLs usable as-is.
        """
        if not isinstance(value, str):
            return value
        if value.startswith("postgres://"):
            return value.replace("postgres://", "postgresql+asyncpg://", 1)
        if value.startswith("postgresql://"):
            return value.replace("postgresql://", "postgresql+asyncpg://", 1)
        return value

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Allow a comma separated string in .env files."""
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("FRONTEND_URL")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @property
    def async_database_url(self) -> str:
        return str(self.DATABASE_URL)

    @property
    def sync_database_url(self) -> str:
        """Blocking DSN for Alembic / data scripts."""
        if self.DATABASE_SYNC_URL:
            return self.DATABASE_SYNC_URL
        return str(self.DATABASE_URL).replace("+asyncpg", "+psycopg2")

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance (module-level singleton)."""
    return Settings()


settings = get_settings()

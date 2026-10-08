from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Upload limits
    MAX_UPLOAD_MB: int = 50
    MAX_UNZIPPED_MB: int = 200
    MAX_ZIP_ENTRIES: int = 50

    # Database
    DATABASE_URL: str = "sqlite+aiosqlite:///./geo_measure.db"

    # Storage
    UPLOAD_DIR: Path = Path("uploads")

    @field_validator("UPLOAD_DIR", mode="before")
    @classmethod
    def _coerce_upload_dir(cls, v: object) -> Path:
        return Path(str(v))

    # Derived helpers (not from env)
    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_MB * 1024 * 1024

    @property
    def max_unzipped_bytes(self) -> int:
        return self.MAX_UNZIPPED_MB * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings instance."""
    return Settings()

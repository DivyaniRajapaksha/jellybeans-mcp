"""Application settings loaded from environment variables and .env."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(PROJECT_ROOT / ".env", ".env"), extra="ignore")

    mongodb_uri: SecretStr
    mongodb_db: str = "test"
    mongodb_max_time_ms: int = 15_000

    mcp_transport: Literal["stdio", "http"] = "stdio"
    host: str | None = None
    port: int = 8000
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    timezone: str = "Asia/Colombo"
    enable_phone_lookup: bool = False

    public_url: str | None = None
    auth0_domain: str | None = None
    auth0_audience: str | None = None


@lru_cache
def get_settings() -> Settings:
    return Settings()

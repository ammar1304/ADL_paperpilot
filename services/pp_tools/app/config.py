"""Service configuration, read from environment variables only."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # postgresql://user:password@host:port/dbname  (the app database, not n8n's)
    database_url: str = Field(alias="APP_DATABASE_URL")
    # Base URL of the Qdrant HTTP API, e.g. http://qdrant:6333
    qdrant_url: str = Field(alias="QDRANT_URL")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    # Seconds allowed for each dependency probe in /health
    health_timeout_s: float = Field(default=2.0, alias="HEALTH_TIMEOUT_S")
    # Seconds to keep retrying the schema setup at startup before giving up
    startup_db_wait_s: float = Field(default=30.0, alias="STARTUP_DB_WAIT_S")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

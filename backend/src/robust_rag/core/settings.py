"""Typed application configuration loaded from environment variables."""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings shared by the API and background workers."""

    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_name: str = Field(default="Robust RAG", validation_alias="APP_NAME")
    app_env: Literal["development", "test", "production"] = Field(
        default="development", validation_alias="APP_ENV"
    )
    api_host: str = Field(default="127.0.0.1", validation_alias="API_HOST")
    api_port: int = Field(default=8000, validation_alias="API_PORT")
    log_level: str = Field(default="INFO", validation_alias="LOG_LEVEL")
    log_format: Literal["console", "json"] = Field(
        default="console", validation_alias="LOG_FORMAT"
    )

    database_url: str = Field(
        default="postgresql+psycopg://localhost:5432/robust_rag",
        validation_alias="DATABASE_URL",
    )
    redis_url: str = Field(
        default="redis://127.0.0.1:6379/0", validation_alias="REDIS_URL"
    )
    celery_result_backend: str = Field(
        default="redis://127.0.0.1:6379/1",
        validation_alias="CELERY_RESULT_BACKEND",
    )
    storage_root: Path = Field(default=Path("../data"), validation_alias="STORAGE_ROOT")

    llm_base_url: str = Field(
        default="http://127.0.0.1:15721/v1", validation_alias="LLM_BASE_URL"
    )
    llm_model: str = Field(default="gpt-5.6-luna", validation_alias="LLM_MODEL")
    llm_api_style: Literal["responses", "chat_completions"] = Field(
        default="responses", validation_alias="LLM_API_STYLE"
    )
    llm_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] = (
        Field(default="medium", validation_alias="LLM_REASONING_EFFORT")
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide immutable settings snapshot."""

    return Settings()

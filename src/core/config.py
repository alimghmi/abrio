from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "sms-gateway"
    app_env: str = "local"
    app_debug: bool = False
    api_prefix: str = "/api"
    log_level: str = "INFO"
    log_json: bool = False

    database_url: str = "postgresql+psycopg://sms_gateway:sms_gateway@localhost:5432/sms_gateway"
    db_create_all: bool = True
    db_pre_start_retries: int = 30
    db_pre_start_interval_seconds: float = 1.0

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "amqp://guest:guest@localhost:5672//"
    celery_result_backend: str = "redis://localhost:6379/1"
    celery_task_always_eager: bool = False
    celery_task_eager_propagates: bool = True


@lru_cache
def get_settings() -> Settings:
    return Settings()

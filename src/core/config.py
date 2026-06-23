from decimal import ROUND_HALF_DOWN, Decimal
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "abrio-gateway"
    app_env: str = "local"
    app_debug: bool = False
    api_prefix: str = "/api"
    log_level: str = "INFO"
    log_json: bool = False

    cost_per_message: Decimal = Decimal("1.00")
    cost_per_express_message: Decimal = Decimal("1.00")
    max_messages_per_batch: int = Field(gt=1, default=100)

    database_url: str = "postgresql+psycopg://sms_gateway:sms_gateway@localhost:5432/sms_gateway"
    db_create_all: bool = True
    db_pre_start_retries: int = 30
    db_pre_start_interval_seconds: float = 1.0

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "amqp://guest:guest@localhost:5672//"
    celery_result_backend: str = "redis://localhost:6379/1"
    celery_task_always_eager: bool = False
    celery_task_eager_propagates: bool = True

    # Dispatch / provider tuning.
    sms_provider: str = "dummy"  # "dummy" (always succeeds) | "mock" (simulates failures)
    sms_mock_fail_rate: float = Field(ge=0.0, le=1.0, default=0.0)
    max_delivery_attempts: int = Field(gt=0, default=5)
    # Express messages older than this are abandoned (a late OTP is useless).
    express_ttl_seconds: int = Field(gt=0, default=120)

    @field_validator("cost_per_message", "cost_per_express_message")
    @classmethod
    def validate_cost(cls, v: Decimal) -> Decimal:
        if v <= Decimal("0"):
            raise ValueError(
                "cost_per_message and cost_per_express_message must be greater than zero"
            )

        return v.quantize(Decimal("0.01"), rounding=ROUND_HALF_DOWN)


@lru_cache
def get_settings() -> Settings:
    return Settings()

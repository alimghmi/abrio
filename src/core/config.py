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
    log_format: str = "console"
    log_request_id_header: str = "X-Request-ID"
    service_name: str | None = None

    cost_per_message: Decimal = Decimal("1.00")
    cost_per_express_message: Decimal = Decimal("1.00")
    max_messages_per_batch: int = Field(gt=1, default=100)

    # Relay dispatch fairness — controls the transactional outbox relay loop.
    # batch_size: max jobs claimed and published per relay loop iteration.
    # per_user_limit: max jobs any single user may occupy in one batch (fairness cap).
    relay_normal_batch_size: int = Field(gt=0, default=80)
    relay_normal_per_user_limit: int = Field(gt=0, default=20)
    relay_express_batch_size: int = Field(gt=0, default=20)
    relay_express_per_user_limit: int = Field(gt=0, default=5)

    sms_provider: str = "dummy"  # "dummy" (always succeeds) | "mock" (simulates failures)
    sms_mock_fail_rate: float = Field(ge=0.0, le=1.0, default=0.0)
    max_delivery_attempts: int = Field(gt=0, default=5)
    # Express messages older than this are abandoned (a late OTP is useless).
    express_ttl_seconds: int = Field(gt=0, default=120)

    rate_limit_enabled: bool = False
    rate_limit_fail_open: bool = True
    rate_limit_key_prefix: str = "sms_gateway:rate_limit"
    rate_limit_redis_url: str | None = None

    # Per-user submission limits — intentionally aligned with relay per_user_limit.
    # Normal: 1,200/min ≈ 20/s matches relay_normal_per_user_limit=20.
    # Express: 300/min ≈ 5/s matches relay_express_per_user_limit=5; burst allows short OTP spikes.
    rate_limit_normal_messages_per_minute: int = Field(gt=0, default=1_200)
    rate_limit_normal_messages_burst: int = Field(gt=0, default=200)
    rate_limit_express_messages_per_minute: int = Field(gt=0, default=300)
    rate_limit_express_messages_burst: int = Field(gt=0, default=100)
    # Batch endpoint: max_messages_per_batch=100 items/request x 1.2/s = ~120/min per user.
    rate_limit_batch_requests_per_minute: int = Field(gt=0, default=120)
    rate_limit_batch_requests_burst: int = Field(gt=0, default=20)

    rate_limit_message_status_per_minute: int = Field(gt=0, default=2_400)
    rate_limit_message_status_burst: int = Field(gt=0, default=400)
    rate_limit_message_reports_per_minute: int = Field(gt=0, default=240)
    rate_limit_message_reports_burst: int = Field(gt=0, default=40)
    rate_limit_user_reads_per_minute: int = Field(gt=0, default=600)
    rate_limit_user_reads_burst: int = Field(gt=0, default=100)
    rate_limit_user_writes_per_minute: int = Field(gt=0, default=60)
    rate_limit_user_writes_burst: int = Field(gt=0, default=15)
    rate_limit_user_creates_per_minute: int = Field(gt=0, default=20)
    rate_limit_user_creates_burst: int = Field(gt=0, default=5)
    rate_limit_pricing_per_minute: int = Field(gt=0, default=240)
    rate_limit_pricing_burst: int = Field(gt=0, default=40)

    # Global limits based on 100M msgs/day ≈ 69,444/min average.
    # 5x peak factor → ~347,000/min total. 80/20 normal/express split:
    #   Normal: 300,000/min, burst 30,000 (≈10% of per-minute window).
    #   Express: 75,000/min, burst 15,000.
    # Batch: system_messages/100 msg per batch ≈ 3,750/min; round to 3,000 + burst 300.
    rate_limit_global_normal_messages_per_minute: int = Field(gt=0, default=300_000)
    rate_limit_global_normal_messages_burst: int = Field(gt=0, default=30_000)
    rate_limit_global_express_messages_per_minute: int = Field(gt=0, default=75_000)
    rate_limit_global_express_messages_burst: int = Field(gt=0, default=15_000)
    rate_limit_global_batch_requests_per_minute: int = Field(gt=0, default=3_000)
    rate_limit_global_batch_requests_burst: int = Field(gt=0, default=300)
    rate_limit_global_message_status_per_minute: int = Field(gt=0, default=120_000)
    rate_limit_global_message_status_burst: int = Field(gt=0, default=12_000)
    rate_limit_global_message_reports_per_minute: int = Field(gt=0, default=12_000)
    rate_limit_global_message_reports_burst: int = Field(gt=0, default=1_200)
    rate_limit_global_user_reads_per_minute: int = Field(gt=0, default=30_000)
    rate_limit_global_user_reads_burst: int = Field(gt=0, default=3_000)
    rate_limit_global_user_writes_per_minute: int = Field(gt=0, default=6_000)
    rate_limit_global_user_writes_burst: int = Field(gt=0, default=600)
    rate_limit_global_user_creates_per_minute: int = Field(gt=0, default=1_200)
    rate_limit_global_user_creates_burst: int = Field(gt=0, default=120)
    rate_limit_global_pricing_per_minute: int = Field(gt=0, default=12_000)
    rate_limit_global_pricing_burst: int = Field(gt=0, default=1_200)

    # System-wide circuit breakers across all tenants combined.
    # Total messages: normal + express = 375,000/min; burst = 37,500.
    # Total API requests ≈ messages + status + reports + reads + writes ≈ 450,000/min.
    rate_limit_system_requests_per_minute: int = Field(gt=0, default=450_000)
    rate_limit_system_requests_burst: int = Field(gt=0, default=45_000)
    rate_limit_system_messages_per_minute: int = Field(gt=0, default=375_000)
    rate_limit_system_messages_burst: int = Field(gt=0, default=37_500)

    database_url: str = "postgresql+psycopg://sms_gateway:sms_gateway@localhost:5432/sms_gateway"
    db_create_all: bool = True
    db_pre_start_retries: int = 30
    db_pre_start_interval_seconds: float = 1.0

    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "amqp://guest:guest@localhost:5672//"
    celery_result_backend: str = "redis://localhost:6379/1"
    celery_task_always_eager: bool = False
    celery_task_eager_propagates: bool = True

    @property
    def use_json_logs(self) -> bool:
        return self.log_format.lower() == "json"

    @property
    def resolved_service_name(self) -> str:
        return self.service_name or self.app_name

    @field_validator("log_format")
    @classmethod
    def validate_log_format(cls, value: str) -> str:
        normalized = value.lower()
        if normalized not in {"console", "json"}:
            raise ValueError("log_format must be 'console' or 'json'")
        return normalized

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

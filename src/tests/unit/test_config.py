from decimal import Decimal

import pytest
from pydantic import ValidationError

from core.config import Settings

pytestmark = pytest.mark.unit


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for env_name in (
        "APP_NAME",
        "APP_ENV",
        "APP_DEBUG",
        "API_PREFIX",
        "LOG_LEVEL",
        "LOG_JSON",
        "DATABASE_URL",
        "DB_CREATE_ALL",
        "REDIS_URL",
        "CELERY_BROKER_URL",
        "CELERY_RESULT_BACKEND",
    ):
        monkeypatch.delenv(env_name, raising=False)

    settings = Settings()

    assert settings.app_name == "abrio-gateway"
    assert settings.api_prefix == "/api"
    assert settings.log_level == "INFO"
    assert settings.log_json is False
    assert settings.db_create_all is True
    assert settings.celery_broker_url.startswith("amqp://")


def test_settings_quantizes_express_message_cost() -> None:
    settings = Settings(cost_per_express_message=Decimal("2.346"))

    assert settings.cost_per_express_message == Decimal("2.35")


@pytest.mark.parametrize("cost", [Decimal("0.00"), Decimal("-1.00")])
def test_settings_rejects_non_positive_express_message_cost(cost: Decimal) -> None:
    with pytest.raises(ValidationError):
        Settings(cost_per_express_message=cost)

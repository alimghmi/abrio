import pytest

from core.config import Settings


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

    assert settings.app_name == "gateway"
    assert settings.api_prefix == "/api"
    assert settings.log_level == "INFO"
    assert settings.log_json is False
    assert settings.db_create_all is True
    assert settings.celery_broker_url.startswith("amqp://")

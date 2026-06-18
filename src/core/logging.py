import logging
from logging.config import dictConfig

from core.config import Settings, get_settings


def configure_logging(settings: Settings | None = None) -> None:
    active_settings = settings or get_settings()
    formatter_name = "json" if active_settings.log_json else "console"

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "console": {
                    "format": "%(asctime)s %(levelname)s [%(name)s] %(message)s",
                },
                "json": {
                    "()": "pythonjsonlogger.json.JsonFormatter",
                    "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": formatter_name,
                },
            },
            "root": {
                "handlers": ["console"],
                "level": active_settings.log_level.upper(),
            },
            "loggers": {
                "uvicorn.access": {
                    "handlers": ["console"],
                    "level": active_settings.log_level.upper(),
                    "propagate": False,
                },
            },
        }
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

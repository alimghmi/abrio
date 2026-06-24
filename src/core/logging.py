import json
import logging
from datetime import UTC, datetime
from logging.config import dictConfig

from core.config import Settings, get_settings
from core.request_context import get_request_id

_LOG_RECORD_FIELDS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
    }
)


class RequestContextFilter(logging.Filter):
    def __init__(self, service_name: str):
        super().__init__()
        self.service_name = service_name

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "service"):
            record.service = self.service_name
        if not hasattr(record, "request_id"):
            record.request_id = get_request_id()
        if not hasattr(record, "event"):
            record.event = record.getMessage()
        return True


class AbrioJsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        record.message = record.getMessage()
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "service": getattr(record, "service", None),
            "logger": record.name,
            "event": getattr(record, "event", record.message),
            "request_id": getattr(record, "request_id", None),
        }

        for key, value in record.__dict__.items():
            if key in _LOG_RECORD_FIELDS or key in payload or key.startswith("_"):
                continue
            if value is not None:
                payload[key] = value

        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, separators=(",", ":"))


def configure_logging(settings: Settings | None = None) -> None:
    active_settings = settings or get_settings()
    formatter_name = "json" if active_settings.use_json_logs else "console"
    service_name = active_settings.resolved_service_name

    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "filters": {
                "request_context": {
                    "()": RequestContextFilter,
                    "service_name": service_name,
                }
            },
            "formatters": {
                "console": {
                    "format": (
                        "%(asctime)s %(levelname)s "
                        "%(service)s [%(name)s] %(event)s request_id=%(request_id)s"
                    ),
                },
                "json": {
                    "()": AbrioJsonFormatter,
                },
            },
            "handlers": {
                "console": {
                    "class": "logging.StreamHandler",
                    "formatter": formatter_name,
                    "filters": ["request_context"],
                },
            },
            "root": {
                "handlers": ["console"],
                "level": active_settings.log_level.upper(),
            },
            "loggers": {
                "uvicorn.access": {
                    "handlers": ["console"],
                    "level": "WARNING",
                    "propagate": False,
                },
            },
        }
    )


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)

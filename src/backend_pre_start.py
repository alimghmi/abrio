import time

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from core.config import get_settings
from core.logging import configure_logging, get_logger
from infra.db.init_db import init_db
from infra.db.session import engine

configure_logging()
logger = get_logger(__name__)


def wait_for_database() -> None:
    settings = get_settings()
    last_error: SQLAlchemyError | None = None

    logger.info("database_wait_started", extra={"retries": settings.db_pre_start_retries})

    for attempt in range(1, settings.db_pre_start_retries + 1):
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            logger.info("database_ready", extra={"attempt": attempt})
            return
        except SQLAlchemyError as exc:
            last_error = exc
            logger.warning("database_not_ready", extra={"attempt": attempt})
            time.sleep(settings.db_pre_start_interval_seconds)

    if last_error is not None:
        raise RuntimeError("Database is not ready") from last_error
    raise RuntimeError("Database is not ready")


if __name__ == "__main__":
    wait_for_database()
    init_db()

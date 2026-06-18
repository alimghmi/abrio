from core.config import get_settings
from core.logging import get_logger
from infra.db import models  # noqa: F401
from infra.db.base import Base
from infra.db.session import engine

logger = get_logger(__name__)


def init_db() -> None:
    settings = get_settings()
    if not settings.db_create_all:
        logger.info("database_create_all_skipped")
        return

    Base.metadata.create_all(bind=engine)
    logger.info("database_tables_ready", extra={"tables": sorted(Base.metadata.tables)})

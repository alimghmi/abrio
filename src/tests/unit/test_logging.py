import logging

import pytest

from core.config import Settings
from core.logging import configure_logging, get_logger

pytestmark = pytest.mark.unit


def test_configure_logging_sets_root_level() -> None:
    configure_logging(Settings(log_level="DEBUG"))

    assert logging.getLogger().level == logging.DEBUG


def test_get_logger_returns_named_logger() -> None:
    logger = get_logger("tests.example")

    assert logger.name == "tests.example"


def test_configure_logging_does_not_duplicate_handlers() -> None:
    configure_logging(Settings(log_level="INFO", log_format="json"))
    configure_logging(Settings(log_level="INFO", log_format="json"))

    assert len(logging.getLogger().handlers) == 1

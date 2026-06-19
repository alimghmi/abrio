import logging

import pytest

from core.config import Settings
from core.logging import configure_logging, get_logger

pytestmark = pytest.mark.unit


def test_configure_logging_sets_root_level() -> None:
    configure_logging(Settings(log_level="DEBUG", log_json=False))

    assert logging.getLogger().level == logging.DEBUG


def test_get_logger_returns_named_logger() -> None:
    logger = get_logger("tests.example")

    assert logger.name == "tests.example"

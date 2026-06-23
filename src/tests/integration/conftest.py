from __future__ import annotations

import os
from collections.abc import Generator

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import infra.db.models  # noqa: F401 — registers ORM models before create_all
from infra.db.base import Base

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://sms_gateway:sms_gateway@localhost:5432/sms_gateway_test",
)

_TRUNCATE_SQL = text(
    "TRUNCATE dispatch_jobs, messages, balances, users RESTART IDENTITY CASCADE"
)


@pytest.fixture(scope="session")
def pg_engine() -> Generator[Engine, None, None]:
    """Create the test database (if absent) and its schema, once per session."""
    _ensure_db_exists(TEST_DATABASE_URL)
    engine = create_engine(TEST_DATABASE_URL, pool_size=40, max_overflow=10, pool_pre_ping=True)
    Base.metadata.create_all(bind=engine)
    yield engine
    engine.dispose()


@pytest.fixture
def pg_sessionmaker(pg_engine: Engine) -> sessionmaker[Session]:
    """Return a session factory, truncating all tables first for a clean slate."""
    with pg_engine.connect() as conn:
        conn.execute(_TRUNCATE_SQL)
        conn.commit()
    return sessionmaker(bind=pg_engine, expire_on_commit=False, class_=Session)


def _ensure_db_exists(url: str) -> None:
    """Create the test database when it does not already exist."""
    db_name = url.rsplit("/", 1)[-1]
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :n"),
                {"n": db_name},
            ).fetchone()
            if exists is None:
                conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    finally:
        admin_engine.dispose()

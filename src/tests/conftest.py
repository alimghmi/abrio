from collections.abc import Callable, Generator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import infra.db.models  # noqa: F401
from infra.db.base import Base
from infra.db.models.balance import Balance
from infra.db.models.user import User

SessionFactory = Callable[[], Session]
SeedUser = Callable[[str, int, int], int]


@pytest.fixture
def sqlite_engine() -> Generator[Engine, None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    try:
        yield engine
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture
def db_session_factory(sqlite_engine: Engine) -> SessionFactory:
    local_session = sessionmaker(
        bind=sqlite_engine,
        expire_on_commit=False,
        class_=Session,
    )

    def create_session() -> Session:
        return local_session()

    return create_session


@pytest.fixture
def seed_user(db_session_factory: SessionFactory) -> SeedUser:
    def create_user(name: str, credits: int = 0, reserved_credits: int = 0) -> int:
        with db_session_factory() as session:
            user = User(
                name=name,
                balance=Balance(credits=credits, reserved_credits=reserved_credits),
            )
            session.add(user)
            session.commit()
            return user.id

    return create_user

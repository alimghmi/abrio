from collections.abc import Callable, Generator
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

import infra.db.models  # noqa: F401
from api.schemas.messages import MessageRequest
from domain.enums import MessagePriority, MessageStatus, PaymentStatus
from infra.db.base import Base
from infra.db.models.balance import Balance
from infra.db.models.message import Message
from infra.db.models.user import User

SessionFactory = Callable[[], Session]
SeedUser = Callable[[str, int, int], int]
MessageRequestFactory = Callable[..., MessageRequest]
SeedMessage = Callable[..., UUID]


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(type_: JSONB, compiler: Any, **kw: Any) -> str:
    return "JSON"


@pytest.fixture
def sqlite_engine() -> Generator[Engine, None, None]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

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


@pytest.fixture
def message_request_factory() -> MessageRequestFactory:
    def create_message_request(
        *,
        user_id: int = 1,
        recipient: str = "+989121234567",
        body: str = "hello",
        priority: MessagePriority = MessagePriority.NORMAL,
        idempotency_key: UUID | None = None,
    ) -> MessageRequest:
        return MessageRequest(
            user_id=user_id,
            recipient=recipient,
            body=body,
            priority=priority,
            idempotency_key=idempotency_key or uuid4(),
        )

    return create_message_request


@pytest.fixture
def seed_message(db_session_factory: SessionFactory) -> SeedMessage:
    def create_message(
        *,
        user_id: int,
        recipient: str = "+989121234567",
        body: str = "hello",
        cost: int = 1,
        priority: MessagePriority = MessagePriority.NORMAL,
        idempotency_key: UUID | None = None,
        status: MessageStatus = MessageStatus.QUEUED,
        payment_status: PaymentStatus = PaymentStatus.RESERVED,
    ) -> UUID:
        with db_session_factory() as session:
            message = Message(
                user_id=user_id,
                recipient=recipient,
                body=body,
                cost=cost,
                priority=priority,
                idempotency_key=idempotency_key or uuid4(),
                status=status,
                payment_status=payment_status,
            )
            session.add(message)
            session.commit()
            return message.id

    return create_message

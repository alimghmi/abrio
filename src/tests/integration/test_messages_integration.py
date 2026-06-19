import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.routes import messages as message_routes
from api.schemas.pagination import PaginationParams
from app.usecases.messages import MessageUseCase
from core.config import Settings
from domain.enums import DispatchJobStatus, MessagePriority, MessageStatus, PaymentStatus
from domain.errors import MessageNotFoundError
from infra.db.models.balance import Balance
from infra.db.models.dispatch_job import DispatchJob
from infra.db.models.message import Message
from tests.conftest import MessageRequestFactory, SeedUser, SessionFactory

pytestmark = pytest.mark.integration


def count_rows(session: Session, model: type[object]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def get_balance(session: Session, user_id: int) -> Balance:
    balance = session.scalar(select(Balance).where(Balance.user_id == user_id))
    assert balance is not None
    return balance


def test_message_endpoint_flow_persists_message_reserves_balance_and_creates_dispatch_job(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    message_request_factory: MessageRequestFactory,
) -> None:
    user_id = seed_user("Ada", credits=5, reserved_credits=1)
    payload = message_request_factory(
        user_id=user_id,
        recipient="09121234567",
        body="hello Ada",
        priority=MessagePriority.EXPRESS,
    )
    settings = Settings(cost_per_message=2)

    with db_session_factory() as session:
        created = asyncio.run(
            message_routes.send_message(payload, MessageUseCase(session, settings))
        )
        message_id = created.id

        assert created.user_id == user_id
        assert created.recipient == "09121234567"
        assert created.body == "hello Ada"
        assert created.cost == 2
        assert created.status == MessageStatus.QUEUED
        assert created.payment_status == PaymentStatus.RESERVED

    with db_session_factory() as session:
        messages = list(session.scalars(select(Message)).all())
        jobs = list(session.scalars(select(DispatchJob)).all())
        balance = get_balance(session, user_id)

        assert len(messages) == 1
        assert messages[0].id == message_id
        assert messages[0].priority == MessagePriority.EXPRESS
        assert messages[0].idempotency_key == payload.idempotency_key
        assert balance.credits == 5
        assert balance.reserved_credits == 3
        assert balance.available_credits == 2
        assert len(jobs) == 1
        assert jobs[0].message_id == message_id
        assert jobs[0].priority == MessagePriority.EXPRESS
        assert jobs[0].status == DispatchJobStatus.PENDING
        assert jobs[0].payload["message_id"] == str(message_id)
        assert jobs[0].payload["user_id"] == user_id
        assert jobs[0].payload["body"] == "hello Ada"
        assert "idempotency_key" not in jobs[0].payload

    with db_session_factory() as session:
        page = asyncio.run(
            message_routes.get_messages(
                user_id=user_id,
                params=PaginationParams(page=1, size=10),
                usecase=MessageUseCase(session, settings),
            )
        )

        assert page.total == 1
        assert page.items[0].id == message_id

    with db_session_factory() as session:
        fetched = asyncio.run(
            message_routes.get_message_by_id(
                message_id,
                user_id=user_id,
                usecase=MessageUseCase(session, settings),
            )
        )

        assert fetched.id == message_id
        assert fetched.user_id == user_id

    with db_session_factory() as session:
        summary = asyncio.run(
            message_routes.get_user_messages_summary(
                user_id,
                usecase=MessageUseCase(session, settings),
            )
        )

        assert summary == {
            "user_id": user_id,
            "total": 1,
            "queued": 1,
            "dispatching": 0,
            "failed": 0,
            "sent": 0,
            "permanent_failed": 0,
        }


def test_message_creation_rolls_back_when_second_message_would_double_reserve_credits(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    message_request_factory: MessageRequestFactory,
) -> None:
    user_id = seed_user("Grace", credits=1, reserved_credits=0)
    settings = Settings(cost_per_message=1)

    with db_session_factory() as session:
        asyncio.run(
            message_routes.send_message(
                message_request_factory(user_id=user_id),
                MessageUseCase(session, settings),
            )
        )

    with db_session_factory() as session, pytest.raises(IntegrityError):
        asyncio.run(
            message_routes.send_message(
                message_request_factory(user_id=user_id),
                MessageUseCase(session, settings),
            )
        )

    with db_session_factory() as session:
        balance = get_balance(session, user_id)

        assert balance.credits == 1
        assert balance.reserved_credits == 1
        assert balance.available_credits == 0
        assert count_rows(session, Message) == 1
        assert count_rows(session, DispatchJob) == 1


def test_duplicate_idempotency_key_does_not_double_charge_under_sqlite(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    message_request_factory: MessageRequestFactory,
) -> None:
    user_id = seed_user("Linus", credits=3, reserved_credits=0)
    idempotency_key = uuid4()
    settings = Settings(cost_per_message=1)

    with db_session_factory() as session:
        asyncio.run(
            message_routes.send_message(
                message_request_factory(user_id=user_id, idempotency_key=idempotency_key),
                MessageUseCase(session, settings),
            )
        )

    with db_session_factory() as session, pytest.raises(IntegrityError):
        asyncio.run(
            message_routes.send_message(
                message_request_factory(user_id=user_id, idempotency_key=idempotency_key),
                MessageUseCase(session, settings),
            )
        )

    with db_session_factory() as session:
        balance = get_balance(session, user_id)

        assert balance.credits == 3
        assert balance.reserved_credits == 1
        assert balance.available_credits == 2
        assert count_rows(session, Message) == 1
        assert count_rows(session, DispatchJob) == 1


def test_get_message_by_id_raises_for_wrong_user_or_missing_message(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    message_request_factory: MessageRequestFactory,
) -> None:
    user_id = seed_user("Ada", credits=2, reserved_credits=0)
    other_user_id = seed_user("Grace", credits=2, reserved_credits=0)
    settings = Settings(cost_per_message=1)

    with db_session_factory() as session:
        created = asyncio.run(
            message_routes.send_message(
                message_request_factory(user_id=user_id),
                MessageUseCase(session, settings),
            )
        )

    with db_session_factory() as session, pytest.raises(MessageNotFoundError):
        asyncio.run(
            message_routes.get_message_by_id(
                created.id,
                user_id=other_user_id,
                usecase=MessageUseCase(session, settings),
            )
        )

    with db_session_factory() as session, pytest.raises(MessageNotFoundError):
        asyncio.run(
            message_routes.get_message_by_id(
                uuid4(),
                user_id=user_id,
                usecase=MessageUseCase(session, settings),
            )
        )


def test_create_message_for_missing_user_rolls_back(
    db_session_factory: SessionFactory,
    message_request_factory: MessageRequestFactory,
) -> None:
    with db_session_factory() as session, pytest.raises(IntegrityError):
        asyncio.run(
            message_routes.send_message(
                message_request_factory(user_id=404),
                MessageUseCase(session, Settings(cost_per_message=1)),
            )
        )

    with db_session_factory() as session:
        assert count_rows(session, Message) == 0
        assert count_rows(session, DispatchJob) == 0


def test_insufficient_balance_rolls_back_without_changing_balance(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    message_request_factory: MessageRequestFactory,
) -> None:
    user_id = seed_user("Alan", credits=0, reserved_credits=0)

    with db_session_factory() as session, pytest.raises(IntegrityError):
        asyncio.run(
            message_routes.send_message(
                message_request_factory(user_id=user_id),
                MessageUseCase(session, Settings(cost_per_message=1)),
            )
        )

    with db_session_factory() as session:
        balance = get_balance(session, user_id)

        assert balance.credits == 0
        assert balance.reserved_credits == 0
        assert count_rows(session, Message) == 0
        assert count_rows(session, DispatchJob) == 0

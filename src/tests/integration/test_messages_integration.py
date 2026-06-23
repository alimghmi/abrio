from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.routes import messages as message_routes
from api.schemas.messages import BatchMessageRequest
from api.schemas.pagination import PaginationParams
from app.usecases.messages import MessageUseCase
from core.config import Settings
from domain.enums import DispatchJobStatus, MessagePriority, MessageStatus, PaymentStatus
from domain.errors import IdempotencyDuplicateError, MessageNotFoundError
from infra.db.models.balance import Balance
from infra.db.models.dispatch_job import DispatchJob
from infra.db.models.message import Message
from tests.conftest import MessageRequestFactory, SeedMessage, SeedUser, SessionFactory

pytestmark = pytest.mark.integration


def count_rows(session: Session, model: type[object]) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


def get_balance(session: Session, user_id: int) -> Balance:
    balance = session.scalar(select(Balance).where(Balance.user_id == user_id))
    assert balance is not None
    return balance


def make_batch_request(
    *,
    user_id: int,
    count: int,
    idempotency_keys: list[UUID] | None = None,
) -> BatchMessageRequest:
    keys = idempotency_keys or [uuid4() for _ in range(count)]
    return BatchMessageRequest(
        user_id=user_id,
        messages=[
            {
                "recipient": "+989121234567",
                "body": f"hello batch {index}",
                "priority": MessagePriority.NORMAL,
                "idempotency_key": keys[index],
            }
            for index in range(count)
        ],
    )


def make_batch_request_with_priorities(
    *,
    user_id: int,
    priorities: list[MessagePriority],
) -> BatchMessageRequest:
    return BatchMessageRequest(
        user_id=user_id,
        messages=[
            {
                "recipient": "+989121234567",
                "body": f"hello {priority.value} {index}",
                "priority": priority,
                "idempotency_key": uuid4(),
            }
            for index, priority in enumerate(priorities)
        ],
    )


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
    settings = Settings(cost_per_express_message=Decimal("2.00"))

    with db_session_factory() as session:
        created = message_routes.send_message(payload, MessageUseCase(session, settings))
        message_id = created.id

        assert created.user_id == user_id
        assert created.recipient == "09121234567"
        assert created.body == "hello Ada"
        assert created.cost == Decimal("2.00")
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
        assert balance.credits == Decimal("5.00")
        assert balance.reserved_credits == Decimal("3.00")
        assert balance.available_credits == Decimal("2.00")
        assert len(jobs) == 1
        assert jobs[0].message_id == message_id
        assert jobs[0].priority == MessagePriority.EXPRESS
        assert jobs[0].status == DispatchJobStatus.PENDING
        assert jobs[0].payload["message_id"] == str(message_id)
        assert jobs[0].payload["user_id"] == user_id
        assert jobs[0].payload["body"] == "hello Ada"
        assert jobs[0].payload["cost"] == "2.00"
        assert "idempotency_key" not in jobs[0].payload

    with db_session_factory() as session:
        page = message_routes.get_messages(
            user_id=user_id,
            params=PaginationParams(page=1, size=10),
            usecase=MessageUseCase(session, settings),
        )

        assert page.total == 1
        assert page.items[0].id == message_id

    with db_session_factory() as session:
        fetched = message_routes.get_message_by_id(
            message_id,
            user_id=user_id,
            usecase=MessageUseCase(session, settings),
        )

        assert fetched.id == message_id
        assert fetched.user_id == user_id

    with db_session_factory() as session:
        summary = message_routes.get_user_messages_summary(
            user_id,
            usecase=MessageUseCase(session, settings),
        )

        assert summary["user_id"] == user_id
        assert summary["total"] == 1
        assert summary["message_status"] == {
            "queued": 1,
            "dispatching": 0,
            "failed": 0,
            "sent": 0,
            "permanent_failed": 0,
        }


def test_batch_message_endpoint_creates_all_messages_jobs_and_reserves_total_credits(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    user_id = seed_user("Batch Ada", credits=5, reserved_credits=1)
    payload = make_batch_request(user_id=user_id, count=2)
    settings = Settings(cost_per_message=Decimal("2.00"))

    with db_session_factory() as session:
        response = message_routes.batch_send_message(payload, MessageUseCase(session, settings))

        assert response.created_count == 2
        assert len(response.messages) == 2
        assert [message.body for message in response.messages] == [
            "hello batch 0",
            "hello batch 1",
        ]

    with db_session_factory() as session:
        messages = list(session.scalars(select(Message).order_by(Message.body)).all())
        jobs = list(session.scalars(select(DispatchJob)).all())
        balance = get_balance(session, user_id)

        assert len(messages) == 2
        assert all(message.user_id == user_id for message in messages)
        assert all(message.cost == Decimal("2.00") for message in messages)
        assert all(message.status == MessageStatus.QUEUED for message in messages)
        assert balance.credits == Decimal("5.00")
        assert balance.reserved_credits == Decimal("5.00")
        assert balance.available_credits == Decimal("0.00")
        assert len(jobs) == 2
        assert {job.message_id for job in jobs} == {message.id for message in messages}
        assert all(job.payload["cost"] == "2.00" for job in jobs)
        assert all("idempotency_key" not in job.payload for job in jobs)


def test_mixed_batch_message_endpoint_uses_priority_based_costs(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    user_id = seed_user("Mixed Batch Ada", credits=10, reserved_credits=1)
    payload = make_batch_request_with_priorities(
        user_id=user_id,
        priorities=[
            MessagePriority.NORMAL,
            MessagePriority.EXPRESS,
            MessagePriority.NORMAL,
        ],
    )
    settings = Settings(
        cost_per_message=Decimal("1.00"),
        cost_per_express_message=Decimal("3.00"),
    )

    with db_session_factory() as session:
        response = message_routes.batch_send_message(payload, MessageUseCase(session, settings))

        assert response.created_count == 3
        assert [message.cost for message in response.messages] == [
            Decimal("1.00"),
            Decimal("3.00"),
            Decimal("1.00"),
        ]

    with db_session_factory() as session:
        messages = list(session.scalars(select(Message)).all())
        jobs = list(session.scalars(select(DispatchJob)).all())
        balance = get_balance(session, user_id)

        message_cost_by_body = {message.body: message.cost for message in messages}
        job_cost_by_body = {job.payload["body"]: job.payload["cost"] for job in jobs}

        assert message_cost_by_body == {
            "hello normal 0": Decimal("1.00"),
            "hello express 1": Decimal("3.00"),
            "hello normal 2": Decimal("1.00"),
        }
        assert balance.credits == Decimal("10.00")
        assert balance.reserved_credits == Decimal("6.00")
        assert balance.available_credits == Decimal("4.00")
        assert len(jobs) == 3
        assert {job.message_id for job in jobs} == {message.id for message in messages}
        assert job_cost_by_body == {
            "hello normal 0": "1.00",
            "hello express 1": "3.00",
            "hello normal 2": "1.00",
        }
        assert all("idempotency_key" not in job.payload for job in jobs)


def test_batch_message_endpoint_rolls_back_when_total_cost_exceeds_available_balance(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    user_id = seed_user("Batch Grace", credits=2, reserved_credits=0)
    payload = make_batch_request(user_id=user_id, count=3)

    with db_session_factory() as session, pytest.raises(IntegrityError):
        message_routes.batch_send_message(
            payload,
            MessageUseCase(session, Settings(cost_per_message=Decimal("1.00"))),
        )

    with db_session_factory() as session:
        balance = get_balance(session, user_id)

        assert balance.credits == Decimal("2.00")
        assert balance.reserved_credits == Decimal("0.00")
        assert count_rows(session, Message) == 0
        assert count_rows(session, DispatchJob) == 0


def test_express_message_insufficient_balance_rolls_back_without_changing_balance(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    message_request_factory: MessageRequestFactory,
) -> None:
    user_id = seed_user("Express Alan", credits=1, reserved_credits=0)

    with db_session_factory() as session, pytest.raises(IntegrityError):
        message_routes.send_message(
            message_request_factory(user_id=user_id, priority=MessagePriority.EXPRESS),
            MessageUseCase(
                session,
                Settings(
                    cost_per_message=Decimal("1.00"),
                    cost_per_express_message=Decimal("2.00"),
                ),
            ),
        )

    with db_session_factory() as session:
        balance = get_balance(session, user_id)

        assert balance.credits == Decimal("1.00")
        assert balance.reserved_credits == Decimal("0.00")
        assert count_rows(session, Message) == 0
        assert count_rows(session, DispatchJob) == 0


def test_batch_message_endpoint_rolls_back_for_duplicate_idempotency_key_in_payload(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    user_id = seed_user("Batch Linus", credits=5, reserved_credits=0)
    duplicate_key = uuid4()
    payload = make_batch_request(
        user_id=user_id,
        count=2,
        idempotency_keys=[duplicate_key, duplicate_key],
    )

    with db_session_factory() as session, pytest.raises(IdempotencyDuplicateError) as exc_info:
        message_routes.batch_send_message(
            payload,
            MessageUseCase(session, Settings(cost_per_message=Decimal("1.00"))),
        )

    assert exc_info.value.idempotency_id == duplicate_key

    with db_session_factory() as session:
        balance = get_balance(session, user_id)

        assert balance.credits == Decimal("5.00")
        assert balance.reserved_credits == Decimal("0.00")
        assert count_rows(session, Message) == 0
        assert count_rows(session, DispatchJob) == 0


def test_batch_message_endpoint_rolls_back_for_existing_idempotency_key(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    seed_message: SeedMessage,
) -> None:
    user_id = seed_user("Batch Existing", credits=5, reserved_credits=0)
    existing_key = uuid4()
    seed_message(user_id=user_id, idempotency_key=existing_key)
    payload = make_batch_request(
        user_id=user_id,
        count=2,
        idempotency_keys=[uuid4(), existing_key],
    )

    with db_session_factory() as session, pytest.raises(IntegrityError):
        message_routes.batch_send_message(
            payload,
            MessageUseCase(session, Settings(cost_per_message=Decimal("1.00"))),
        )

    with db_session_factory() as session:
        balance = get_balance(session, user_id)

        assert balance.credits == Decimal("5.00")
        assert balance.reserved_credits == Decimal("0.00")
        assert count_rows(session, Message) == 1
        assert count_rows(session, DispatchJob) == 0


def test_get_message_by_id_raises_for_wrong_user_or_missing_message(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    seed_message: SeedMessage,
) -> None:
    user_id = seed_user("Ada", credits=2, reserved_credits=0)
    other_user_id = seed_user("Grace", credits=2, reserved_credits=0)
    settings = Settings(cost_per_message=1)
    message_id = seed_message(user_id=user_id)

    with db_session_factory() as session, pytest.raises(MessageNotFoundError):
        message_routes.get_message_by_id(
            message_id,
            user_id=other_user_id,
            usecase=MessageUseCase(session, settings),
        )

    with db_session_factory() as session, pytest.raises(MessageNotFoundError):
        message_routes.get_message_by_id(
            uuid4(),
            user_id=user_id,
            usecase=MessageUseCase(session, settings),
        )


def test_insufficient_balance_rolls_back_without_changing_balance(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    message_request_factory: MessageRequestFactory,
) -> None:
    user_id = seed_user("Alan", credits=0, reserved_credits=0)

    with db_session_factory() as session, pytest.raises(IntegrityError):
        message_routes.send_message(
            message_request_factory(user_id=user_id),
            MessageUseCase(session, Settings(cost_per_message=Decimal("1.00"))),
        )

    with db_session_factory() as session:
        balance = get_balance(session, user_id)

        assert balance.credits == Decimal("0.00")
        assert balance.reserved_credits == Decimal("0.00")
        assert count_rows(session, Message) == 0
        assert count_rows(session, DispatchJob) == 0

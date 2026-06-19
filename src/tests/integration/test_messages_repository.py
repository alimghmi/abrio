from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from domain.enums import MessagePriority, MessageStatus, PaymentStatus
from domain.errors import MessageNotFoundError
from infra.db.models.dispatch_job import DispatchJob
from infra.db.models.message import Message
from infra.db.repositories.dispatch_jobs import DispatchJobRepository
from infra.db.repositories.messages import MessageRepository
from tests.conftest import MessageRequestFactory, SeedMessage, SeedUser, SessionFactory

pytestmark = pytest.mark.integration


def message_payload(
    message_request_factory: MessageRequestFactory,
    *,
    user_id: int,
    cost: int = 1,
    priority: MessagePriority = MessagePriority.NORMAL,
    idempotency_key: UUID | None = None,
) -> dict[str, object]:
    request = message_request_factory(
        user_id=user_id,
        priority=priority,
        idempotency_key=idempotency_key,
    )
    return {**request.model_dump(), "cost": cost}


def test_message_repository_create_and_get_message(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    message_request_factory: MessageRequestFactory,
) -> None:
    user_id = seed_user("Ada", credits=3, reserved_credits=0)
    payload = message_payload(message_request_factory, user_id=user_id, cost=2)

    with db_session_factory() as session:
        message = MessageRepository(session).create_message(payload)
        session.commit()
        session.refresh(message)
        message_id = message.id

        assert message.user_id == user_id
        assert message.cost == 2
        assert message.status == MessageStatus.QUEUED
        assert message.payment_status == PaymentStatus.RESERVED

    with db_session_factory() as session:
        fetched = MessageRepository(session).get_user_message(message_id, user_id)

        assert fetched.id == message_id
        assert fetched.idempotency_key == payload["idempotency_key"]


def test_message_repository_get_user_message_by_idempotency_key(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    seed_message: SeedMessage,
) -> None:
    user_id = seed_user("Ada", credits=3, reserved_credits=0)
    idempotency_key = uuid4()
    message_id = seed_message(user_id=user_id, idempotency_key=idempotency_key)

    with db_session_factory() as session:
        message = MessageRepository(session).get_user_message_by_idempotency_key(
            user_id,
            idempotency_key,
        )

        assert message.id == message_id


def test_message_repository_get_messages_slice_filters_and_paginates(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    seed_message: SeedMessage,
) -> None:
    ada_id = seed_user("Ada", credits=10, reserved_credits=0)
    grace_id = seed_user("Grace", credits=10, reserved_credits=0)
    matching_message_id = seed_message(
        user_id=ada_id,
        priority=MessagePriority.EXPRESS,
        status=MessageStatus.SENT,
        payment_status=PaymentStatus.DEDUCTED,
    )
    seed_message(
        user_id=ada_id,
        priority=MessagePriority.NORMAL,
        status=MessageStatus.FAILED,
        payment_status=PaymentStatus.RESERVED,
    )
    seed_message(
        user_id=grace_id,
        priority=MessagePriority.EXPRESS,
        status=MessageStatus.SENT,
        payment_status=PaymentStatus.DEDUCTED,
    )

    with db_session_factory() as session:
        messages, total = MessageRepository(session).get_messages_slice(
            limit=10,
            offset=0,
            user_id=ada_id,
            status=MessageStatus.SENT,
            priority=MessagePriority.EXPRESS,
            payment_status=PaymentStatus.DEDUCTED,
        )

        assert total == 1
        assert messages[0].id == matching_message_id

    with db_session_factory() as session:
        messages, total = MessageRepository(session).get_messages_slice(
            limit=1,
            offset=1,
            user_id=ada_id,
        )

        assert total == 2
        assert len(messages) == 1


def test_message_repository_updates_status_and_payment_status(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    seed_message: SeedMessage,
) -> None:
    user_id = seed_user("Ada", credits=3, reserved_credits=0)
    message_id = seed_message(user_id=user_id)

    with db_session_factory() as session:
        repo = MessageRepository(session)
        message = repo.update_message_status(message_id, MessageStatus.SENT)
        payment = repo.update_message_payment_status(message_id, PaymentStatus.DEDUCTED)
        session.commit()

        assert message.status == MessageStatus.SENT
        assert payment.payment_status == PaymentStatus.DEDUCTED

    with db_session_factory() as session:
        persisted = session.get(Message, message_id)

        assert persisted is not None
        assert persisted.status == MessageStatus.SENT
        assert persisted.payment_status == PaymentStatus.DEDUCTED


def test_message_repository_calculates_summary(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    seed_message: SeedMessage,
) -> None:
    user_id = seed_user("Ada", credits=10, reserved_credits=0)
    other_user_id = seed_user("Grace", credits=10, reserved_credits=0)
    seed_message(user_id=user_id, status=MessageStatus.QUEUED)
    seed_message(user_id=user_id, status=MessageStatus.DISPATCHING)
    seed_message(user_id=user_id, status=MessageStatus.FAILED)
    seed_message(user_id=user_id, status=MessageStatus.SENT)
    seed_message(user_id=user_id, status=MessageStatus.PERMANENT_FAILED)
    seed_message(user_id=other_user_id, status=MessageStatus.SENT)

    with db_session_factory() as session:
        summary = MessageRepository(session).calculate_summary(user_id)

        assert summary == {
            "user_id": user_id,
            "total": 5,
            "queued": 1,
            "dispatching": 1,
            "failed": 1,
            "sent": 1,
            "permanent_failed": 1,
        }


def test_message_repository_raises_for_missing_messages(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    user_id = seed_user("Ada", credits=3, reserved_credits=0)
    missing_message_id = uuid4()

    with db_session_factory() as session:
        repo = MessageRepository(session)

        with pytest.raises(MessageNotFoundError):
            repo.get_user_message(missing_message_id, user_id)

        with pytest.raises(MessageNotFoundError):
            repo.get_user_message_by_idempotency_key(user_id, uuid4())

        with pytest.raises(MessageNotFoundError):
            repo._get_message(missing_message_id)

        with pytest.raises(MessageNotFoundError):
            repo.update_message_status(missing_message_id, MessageStatus.SENT)

        with pytest.raises(MessageNotFoundError):
            repo.update_message_payment_status(missing_message_id, PaymentStatus.DEDUCTED)


def test_dispatch_job_repository_create_shapes_payload(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    seed_message: SeedMessage,
    message_request_factory: MessageRequestFactory,
) -> None:
    user_id = seed_user("Ada", credits=3, reserved_credits=0)
    message_id = seed_message(user_id=user_id)
    request = message_request_factory(user_id=user_id, priority=MessagePriority.EXPRESS)
    payload = request.model_dump()

    with db_session_factory() as session:
        job = DispatchJobRepository(session).create(
            message_id=message_id,
            priority=MessagePriority.EXPRESS,
            payload=payload,
        )
        session.commit()
        session.refresh(job)
        job_id = job.id

        assert "idempotency_key" not in payload

    with db_session_factory() as session:
        persisted = session.scalar(select(DispatchJob).where(DispatchJob.id == job_id))

        assert persisted is not None
        assert persisted.message_id == message_id
        assert persisted.priority == MessagePriority.EXPRESS
        assert persisted.payload["message_id"] == str(message_id)
        assert persisted.payload["user_id"] == user_id
        assert persisted.payload["priority"] == MessagePriority.EXPRESS
        assert "idempotency_key" not in persisted.payload

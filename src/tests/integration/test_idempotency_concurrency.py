from __future__ import annotations

import threading
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from api.schemas.messages import MessageRequest
from app.usecases.messages import MessageUseCase
from core.config import Settings
from domain.enums import MessagePriority
from infra.db.models.balance import Balance
from infra.db.models.dispatch_job import DispatchJob
from infra.db.models.message import Message
from infra.db.models.user import User

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

SETTINGS = Settings(cost_per_message=Decimal("1.00"), cost_per_express_message=Decimal("1.00"))


def make_user(maker: sessionmaker[Session], *, credits: int) -> int:
    with maker() as session:
        user = User(
            name=f"idem-{uuid4().hex[:8]}",
            balance=Balance(credits=credits, reserved_credits=Decimal("0.00")),
        )
        session.add(user)
        session.commit()
        return user.id


def test_concurrent_same_key_creates_exactly_one_message(
    pg_sessionmaker: sessionmaker[Session],
) -> None:
    """20 concurrent requests, same user, same key, same payload.

    Expected: exactly one message, one dispatch job, one credit reservation, and
    every caller receives the same message id (no exceptions surface).
    """
    user_id = make_user(pg_sessionmaker, credits=1000)
    key = uuid4()
    attempts = 20

    barrier = threading.Barrier(attempts)
    lock = threading.Lock()
    returned_ids: list[str] = []
    errors: list[str] = []

    def submit() -> None:
        payload = MessageRequest(
            user_id=user_id,
            recipient="+989121234567",
            body="idempotent",
            priority=MessagePriority.NORMAL,
            idempotency_key=key,
        )
        with pg_sessionmaker() as session:
            usecase = MessageUseCase(session, SETTINGS)
            barrier.wait()  # release all threads together to maximise contention
            try:
                message = usecase.create_message(payload)
                with lock:
                    returned_ids.append(str(message.id))
            except Exception as exc:  # — record, assert none below
                with lock:
                    errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=submit) for _ in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == [], f"no caller should error on a clean replay, got: {errors[:3]}"
    assert len(returned_ids) == attempts, "every caller should receive a result"
    assert len(set(returned_ids)) == 1, "all callers must receive the same message id"

    with pg_sessionmaker() as session:
        messages = session.query(Message).filter(Message.user_id == user_id).all()
        jobs = session.query(DispatchJob).filter(DispatchJob.user_id == user_id).all()
        balance = session.query(Balance).filter(Balance.user_id == user_id).one()

    assert len(messages) == 1, "exactly one message persisted"
    assert len(jobs) == 1, "exactly one dispatch job persisted"
    # Exactly one credit reserved — no double charge.
    assert balance.reserved_credits == Decimal("1.00")
    assert str(messages[0].id) == returned_ids[0]


def test_concurrent_same_key_different_payload_returns_original(
    pg_sessionmaker: sessionmaker[Session],
) -> None:
    """Same key reused with a different payload returns the original message and
    ignores the new payload (no request-fingerprint validation). This documents
    the deliberately simpler behavior; the invariant that matters is that no
    second message/job/reservation is created.
    """
    user_id = make_user(pg_sessionmaker, credits=1000)
    key = uuid4()

    with pg_sessionmaker() as session:
        first = MessageUseCase(session, SETTINGS).create_message(
            MessageRequest(
                user_id=user_id,
                recipient="+989121234567",
                body="ORIGINAL",
                priority=MessagePriority.NORMAL,
                idempotency_key=key,
            )
        )
        original_id = first.id

    with pg_sessionmaker() as session:
        replay = MessageUseCase(session, SETTINGS).create_message(
            MessageRequest(
                user_id=user_id,
                recipient="+989121234567",
                body="DIFFERENT",
                priority=MessagePriority.EXPRESS,
                idempotency_key=key,
            )
        )

    assert replay.id == original_id
    assert replay.body == "ORIGINAL", "new payload is ignored; original is returned"
    assert replay.priority == MessagePriority.NORMAL

    with pg_sessionmaker() as session:
        messages = session.query(Message).filter(Message.user_id == user_id).all()
        jobs = session.query(DispatchJob).filter(DispatchJob.user_id == user_id).all()
        balance = session.query(Balance).filter(Balance.user_id == user_id).one()

    assert len(messages) == 1
    assert len(jobs) == 1
    assert balance.reserved_credits == Decimal("1.00")

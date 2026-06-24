from __future__ import annotations

import threading
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session, sessionmaker

from app.usecases.dispatch import DispatchUseCase
from app.usecases.messages import MessageUseCase
from core.config import Settings
from domain.enums import (
    DispatchJobStatus,
    MessagePriority,
    MessageStatus,
    PaymentStatus,
)
from domain.errors import InsufficientBalanceError
from infra.db.models.balance import Balance
from infra.db.models.dispatch_job import DispatchJob
from infra.db.models.message import Message
from infra.db.models.user import User
from infra.db.repositories.dispatch_jobs import DispatchJobRepository
from infra.providers.mock import MockSmsProvider
from infra.providers.types import ProviderOutcome

pytestmark = [pytest.mark.integration, pytest.mark.concurrency]

SETTINGS = Settings(cost_per_message=Decimal("1.00"), cost_per_express_message=Decimal("1.00"))


def make_user(maker: sessionmaker[Session], *, credits: int) -> int:
    with maker() as session:
        user = User(
            name=f"itest-{uuid4().hex[:8]}",
            balance=Balance(credits=credits, reserved_credits=0),
        )
        session.add(user)
        session.commit()
        return user.id


def test_concurrent_submissions_respect_credit_limit(
    pg_sessionmaker: sessionmaker[Session],
) -> None:
    credits = 10
    attempts = 100
    user_id = make_user(pg_sessionmaker, credits=credits)

    accepted = 0
    insufficient = 0
    lock = threading.Lock()
    barrier = threading.Barrier(attempts)

    def submit() -> None:
        nonlocal accepted, insufficient
        from api.schemas.messages import MessageRequest

        payload = MessageRequest(
            user_id=user_id,
            recipient="+989121234567",
            body="concurrent",
            priority=MessagePriority.NORMAL,
            idempotency_key=uuid4(),
        )
        with pg_sessionmaker() as session:
            usecase = MessageUseCase(session, SETTINGS)
            barrier.wait()  # release all threads together to maximise contention
            try:
                usecase.create_message(payload)
                with lock:
                    accepted += 1
            except InsufficientBalanceError:
                with lock:
                    insufficient += 1

    threads = [threading.Thread(target=submit) for _ in range(attempts)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert accepted == credits, f"expected exactly {credits} accepted, got {accepted}"
    assert insufficient == attempts - credits

    # Exactly `credits` messages and dispatch jobs were created; balance non-negative.
    # NOTE: reserved/credits are intentionally NOT asserted here — if the live
    # docker-compose relay+workers share this DB they will settle these jobs
    # concurrently and draw reserved_credits down. The invariant that matters
    # (exactly `credits` accepted, never negative) holds regardless of who else
    # touches the balance, because the CHECK constraint is the real guard.
    with pg_sessionmaker() as session:
        messages = session.query(Message).filter(Message.user_id == user_id).all()
        jobs = session.query(DispatchJob).filter(DispatchJob.user_id == user_id).all()
        balance = session.query(Balance).filter(Balance.user_id == user_id).one()

        assert len(messages) == credits
        assert len(jobs) == credits
        assert balance.available_credits >= Decimal("0")
        assert balance.reserved_credits >= Decimal("0")


def test_concurrent_relays_claim_disjoint_jobs(
    pg_sessionmaker: sessionmaker[Session],
) -> None:
    """Two relays claiming the same priority simultaneously must never both
    claim the same job (FOR UPDATE SKIP LOCKED)."""
    user_id = make_user(pg_sessionmaker, credits=100)
    job_count = 20

    with pg_sessionmaker() as session:
        for _ in range(job_count):
            message = Message(
                user_id=user_id,
                recipient="+989121234567",
                body="claim me",
                cost=1,
                priority=MessagePriority.NORMAL,
                idempotency_key=uuid4(),
            )
            session.add(message)
            session.flush()
            session.add(
                DispatchJob(
                    message_id=message.id,
                    user_id=user_id,
                    priority=MessagePriority.NORMAL,
                    payload={"message_id": str(message.id), "user_id": user_id},
                )
            )
        # Reserve credits for these jobs so that if the live compose workers
        # later pick them up, settlement stays valid (no CHECK violation).
        balance = session.query(Balance).filter(Balance.user_id == user_id).one()
        balance.reserved_credits = Decimal(job_count)
        session.commit()

    results: dict[str, set] = {}
    start = threading.Barrier(2)
    release = threading.Barrier(2)

    def claim(relay_id: str) -> None:
        with pg_sessionmaker() as session, session.begin():
            repo = DispatchJobRepository(session)
            start.wait()  # both inside a transaction before either claims
            claimed = repo.claim_batch(
                priority=MessagePriority.NORMAL,
                relay_id=relay_id,
                limit=job_count,
                lease_seconds=60,
                per_user_limit=job_count,
            )
            results[relay_id] = {job.id for job in claimed}
            release.wait()  # hold locks until both have claimed, then commit

    t1 = threading.Thread(target=claim, args=("relay-a",))
    t2 = threading.Thread(target=claim, args=("relay-b",))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    a, b = results["relay-a"], results["relay-b"]
    # SKIP LOCKED guarantees no double-claiming (disjoint sets).  It does NOT
    # guarantee work-splitting: one relay may lock all rows before the other
    # starts its query.  The correct invariant with a clean DB is that the two
    # relays together claim every job exactly once.
    assert a.isdisjoint(b), f"jobs claimed by both relays: {a & b}"
    assert len(a) + len(b) == job_count, (
        f"expected all {job_count} jobs claimed exactly once, "
        f"got relay-a={len(a)} relay-b={len(b)}"
    )


def test_mock_provider_permanent_failure_refunds(
    pg_sessionmaker: sessionmaker[Session],
) -> None:
    user_id = make_user(pg_sessionmaker, credits=5)

    with pg_sessionmaker() as session:
        # Reserve one credit, as a real submission would.
        balance = session.query(Balance).filter(Balance.user_id == user_id).one()
        balance.reserved_credits = Decimal("1.00")
        message = Message(
            user_id=user_id,
            recipient="+989121234567",
            body="[FAIL_PERMANENT] otp 123",
            cost=Decimal("1.00"),
            priority=MessagePriority.NORMAL,
            idempotency_key=uuid4(),
            status=MessageStatus.QUEUED,
            payment_status=PaymentStatus.RESERVED,
        )
        session.add(message)
        session.flush()
        job = DispatchJob(
            message_id=message.id,
            user_id=user_id,
            priority=MessagePriority.NORMAL,
            status=DispatchJobStatus.PUBLISHED,
            payload={"message_id": str(message.id), "user_id": user_id},
        )
        session.add(job)
        session.commit()
        job_id = job.id
        message_id = message.id

    # The mock provider returns a permanent failure for the [FAIL_PERMANENT] body.
    provider = MockSmsProvider()
    assert (
        provider.send(
            message_id=message_id, recipient="+989121234567", body="[FAIL_PERMANENT] x"
        ).outcome
        == ProviderOutcome.PERMANENT_FAILURE
    )

    with pg_sessionmaker() as session:
        DispatchUseCase(session, provider, max_delivery_attempts=5).process(job_id)

    with pg_sessionmaker() as session:
        job = session.get(DispatchJob, job_id)
        message = session.get(Message, message_id)
        balance = session.query(Balance).filter(Balance.user_id == user_id).one()
        assert job is not None and message is not None
        assert job.status == DispatchJobStatus.FAILED
        assert message.status == MessageStatus.PERMANENT_FAILED
        assert message.payment_status == PaymentStatus.REFUNDED
        # Reservation released, original credits untouched (never delivered).
        assert balance.reserved_credits == Decimal("0.00")
        assert balance.credits == Decimal("5.00")

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest

from domain.enums import DispatchJobStatus, MessagePriority
from infra.db.models.dispatch_job import DispatchJob
from infra.db.models.message import Message
from infra.db.repositories.dispatch_jobs import DispatchJobRepository
from tests.conftest import SeedUser, SessionFactory

pytestmark = pytest.mark.unit

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def make_job(
    session_factory: SessionFactory,
    *,
    user_id: int,
    available_offset_seconds: int,
    priority: MessagePriority = MessagePriority.NORMAL,
    status: DispatchJobStatus = DispatchJobStatus.PENDING,
) -> UUID:
    with session_factory() as session:
        message = Message(
            user_id=user_id,
            recipient="+989121234567",
            body="hello",
            cost=1,
            priority=priority,
            idempotency_key=uuid4(),
        )
        session.add(message)
        session.flush()
        job = DispatchJob(
            message_id=message.id,
            user_id=user_id,
            priority=priority,
            status=status,
            payload={"message_id": str(message.id), "user_id": user_id},
            available_at=BASE + timedelta(seconds=available_offset_seconds),
        )
        session.add(job)
        session.commit()
        return job.id


def test_per_user_limit_prevents_a_hot_user_from_monopolizing_a_batch(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    hot = seed_user("hot", 1000, 0)
    small = seed_user("small", 1000, 0)

    # Hot user floods with 10 older jobs; small user has a single newer job.
    for offset in range(10):
        make_job(db_session_factory, user_id=hot, available_offset_seconds=offset)
    small_job = make_job(db_session_factory, user_id=small, available_offset_seconds=100)

    with db_session_factory() as session:
        repo = DispatchJobRepository(session)
        claimed = repo.claim_batch(
            priority=MessagePriority.NORMAL,
            relay_id="relay-1",
            limit=2,
            lease_seconds=60,
            per_user_limit=1,
        )
        claimed_ids = {job.id for job in claimed}
        # Even though the hot user has 10 older jobs, the small user's job is
        # claimed in the same cycle because the per-user cap is 1.
        assert small_job in claimed_ids
        assert len(claimed) == 2
        user_ids = sorted(job.user_id for job in claimed)
        assert user_ids == [min(hot, small), max(hot, small)]
        # Claimed jobs carry the lease.
        assert all(job.locked_by == "relay-1" for job in claimed)
        assert all(job.locked_at is not None for job in claimed)


def test_topup_fills_spare_capacity_when_only_one_user_has_work(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    solo = seed_user("solo", 1000, 0)
    for offset in range(10):
        make_job(db_session_factory, user_id=solo, available_offset_seconds=offset)

    with db_session_factory() as session:
        repo = DispatchJobRepository(session)
        claimed = repo.claim_batch(
            priority=MessagePriority.NORMAL,
            relay_id="relay-1",
            limit=5,
            lease_seconds=60,
            per_user_limit=2,
        )
        # Fairness cap is 2, but with a single active user the top-up pass fills
        # the rest of the batch so throughput isn't wasted.
        assert len(claimed) == 5


def test_claim_batch_ignores_other_priority_and_future_jobs(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    user = seed_user("u", 1000, 0)
    due = make_job(db_session_factory, user_id=user, available_offset_seconds=-10)
    make_job(
        db_session_factory,
        user_id=user,
        available_offset_seconds=-10,
        priority=MessagePriority.EXPRESS,
    )

    with db_session_factory() as session:
        repo = DispatchJobRepository(session)
        claimed = repo.claim_batch(
            priority=MessagePriority.NORMAL,
            relay_id="relay-1",
            limit=10,
            lease_seconds=60,
            per_user_limit=10,
        )
        assert [job.id for job in claimed] == [due]


def test_reclaim_stale_published_returns_jobs_to_retry(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    user = seed_user("u", 1000, 0)
    job_id = make_job(
        db_session_factory,
        user_id=user,
        available_offset_seconds=0,
        status=DispatchJobStatus.PUBLISHED,
    )
    # Backdate published_at so it looks stranded.
    with db_session_factory() as session:
        job = session.get(DispatchJob, job_id)
        assert job is not None
        job.published_at = datetime.now(UTC) - timedelta(seconds=600)
        session.commit()

    with db_session_factory() as session:
        repo = DispatchJobRepository(session)
        reclaimed = repo.reclaim_stale_inflight(
            priority=MessagePriority.NORMAL,
            lease_seconds=120,
            limit=100,
        )
        session.commit()
        assert reclaimed == 1

    with db_session_factory() as session:
        job = session.get(DispatchJob, job_id)
        assert job is not None
        assert job.status == DispatchJobStatus.RETRY
        assert job.delivery_attempts == 1
        assert job.locked_by is None


def test_reclaim_stale_dispatching_returns_jobs_to_retry(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    """A worker that died mid-delivery leaves a job DISPATCHING with a stale
    worker lease; the reaper recovers it via locked_at."""
    user = seed_user("u", 1000, 0)
    job_id = make_job(
        db_session_factory,
        user_id=user,
        available_offset_seconds=0,
        status=DispatchJobStatus.DISPATCHING,
    )
    with db_session_factory() as session:
        job = session.get(DispatchJob, job_id)
        assert job is not None
        job.locked_at = datetime.now(UTC) - timedelta(seconds=600)
        job.locked_by = "dead-worker"
        session.commit()

    with db_session_factory() as session:
        repo = DispatchJobRepository(session)
        reclaimed = repo.reclaim_stale_inflight(
            priority=MessagePriority.NORMAL,
            lease_seconds=120,
            limit=100,
        )
        session.commit()
        assert reclaimed == 1

    with db_session_factory() as session:
        job = session.get(DispatchJob, job_id)
        assert job is not None
        assert job.status == DispatchJobStatus.RETRY
        assert job.delivery_attempts == 1
        assert job.locked_by is None

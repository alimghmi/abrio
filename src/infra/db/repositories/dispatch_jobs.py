from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import ColumnElement, CursorResult, and_, func, or_, select, update
from sqlalchemy.orm import Session

from domain.enums import DispatchJobStatus, MessagePriority
from infra.db.models.dispatch_job import DispatchJob


class DispatchJobRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(
        self, message_id: UUID, priority: MessagePriority, payload: dict[str, Any]
    ) -> DispatchJob:
        job_payload = {
            "message_id": str(message_id),
            "user_id": payload["user_id"],
            "recipient": payload["recipient"],
            "body": payload["body"],
            "priority": priority.value,
            "cost": str(payload["cost"]),
        }
        job = DispatchJob(
            message_id=message_id,
            user_id=payload["user_id"],
            priority=priority,
            payload=job_payload,
        )
        self.session.add(job)
        return job

    def claim_batch(
        self,
        *,
        priority: MessagePriority,
        relay_id: str,
        limit: int,
        lease_seconds: int,
        per_user_limit: int,
    ) -> list[DispatchJob]:
        """Claim up to `limit` due jobs, fairly across customers.

        Two passes:

        1. Fairness pass: rank each customer's due jobs by age and take at
           most `per_user_limit` per customer, interleaved round-robin
           (every customer's oldest first, then second-oldest, ...). A
           flooding tenant therefore cannot monopolise a batch while other
           tenants have pending work.
        2. Top-up pass: if the fairness cap left spare capacity (e.g. only
           one customer currently has work), fill the remainder FIFO so we
           never waste batch throughput.

        ``FOR UPDATE SKIP LOCKED`` on both passes keeps multiple relay
        replicas safe; the eligibility clause is re-checked while locking to
        avoid acting on a row another replica changed in the meantime.
        """
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=lease_seconds)
        eligible = self._eligible_clause(priority=priority, now=now, stale_before=stale_before)

        ranked = (
            select(
                DispatchJob.id.label("id"),
                func.row_number()
                .over(
                    partition_by=DispatchJob.user_id,
                    order_by=(DispatchJob.available_at, DispatchJob.created_at),
                )
                .label("rn"),
            )
            .where(eligible)
            .subquery()
        )
        fair_ids = (
            select(ranked.c.id)
            .where(ranked.c.rn <= per_user_limit)
            .order_by(ranked.c.rn, ranked.c.id)
            .limit(limit)
        )
        fair_query = (
            select(DispatchJob)
            .where(DispatchJob.id.in_(fair_ids), eligible)
            .order_by(DispatchJob.available_at, DispatchJob.created_at)
            .with_for_update(skip_locked=True)
        )
        jobs = list(self.session.scalars(fair_query).all())

        remaining = limit - len(jobs)
        if remaining > 0:
            claimed_ids = [job.id for job in jobs]
            topup_query = (
                select(DispatchJob)
                .where(eligible, DispatchJob.id.notin_(claimed_ids))
                .order_by(DispatchJob.available_at, DispatchJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(remaining)
            )
            jobs.extend(self.session.scalars(topup_query).all())

        for job in jobs:
            job.locked_at = now
            job.locked_by = relay_id

        return jobs

    @staticmethod
    def _eligible_clause(
        *,
        priority: MessagePriority,
        now: datetime,
        stale_before: datetime,
    ) -> ColumnElement[bool]:
        return and_(
            DispatchJob.priority == priority,
            DispatchJob.status.in_(
                [
                    DispatchJobStatus.PENDING,
                    DispatchJobStatus.RETRY,
                ]
            ),
            DispatchJob.available_at <= now,
            or_(
                DispatchJob.locked_at.is_(None),
                DispatchJob.locked_at < stale_before,
            ),
        )

    def reclaim_stale_inflight(
        self,
        *,
        priority: MessagePriority,
        lease_seconds: int,
        limit: int,
    ) -> int:
        """Recover jobs stranded in-flight back to RETRY so the relay republishes.

        Two strand cases, each keyed off the timestamp that marks when the
        in-flight phase began:
          - PUBLISHED past `lease_seconds` of `published_at`: the worker never
            picked up the message (crash / lost delivery).
          - DISPATCHING past `lease_seconds` of `locked_at`: a worker claimed the
            job for delivery and then died before recording the outcome.

        Counts as a delivery attempt so a job whose worker keeps dying is
        eventually retired by the worker's max-attempts guard instead of
        looping forever. Returns the number of jobs reclaimed.
        """
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=lease_seconds)

        stale_ids = (
            select(DispatchJob.id)
            .where(
                DispatchJob.priority == priority,
                or_(
                    and_(
                        DispatchJob.status == DispatchJobStatus.PUBLISHED,
                        DispatchJob.published_at < stale_before,
                    ),
                    and_(
                        DispatchJob.status == DispatchJobStatus.DISPATCHING,
                        DispatchJob.locked_at < stale_before,
                    ),
                ),
            )
            .limit(limit)
        )
        query = (
            update(DispatchJob)
            .where(DispatchJob.id.in_(stale_ids))
            .values(
                status=DispatchJobStatus.RETRY,
                delivery_attempts=DispatchJob.delivery_attempts + 1,
                available_at=now,
                locked_at=None,
                locked_by=None,
                last_error="reclaimed: stale in-flight lease expired",
            )
        )
        result = cast(CursorResult[Any], self.session.execute(query))
        return result.rowcount or 0

    def mark_published(
        self,
        *,
        job_ids: list[UUID],
        relay_id: str,
    ) -> None:
        if not job_ids:
            return

        now = datetime.now(UTC)
        query = (
            update(DispatchJob)
            .where(
                DispatchJob.id.in_(job_ids),
                DispatchJob.locked_by == relay_id,
                DispatchJob.status.in_(
                    [
                        DispatchJobStatus.PENDING,
                        DispatchJobStatus.RETRY,
                    ]
                ),
            )
            .values(
                status=DispatchJobStatus.PUBLISHED,
                published_at=now,
                locked_at=None,
                locked_by=None,
                last_error=None,
            )
        )

        self.session.execute(query)

    def mark_publish_retry(
        self,
        *,
        job_ids: list[UUID],
        relay_id: str,
        error: str,
        delay_seconds: float,
    ) -> None:
        if not job_ids:
            return

        now = datetime.now(UTC)
        next_attempt = now + timedelta(seconds=delay_seconds)

        query = (
            update(DispatchJob)
            .where(
                DispatchJob.id.in_(job_ids),
                DispatchJob.locked_by == relay_id,
                DispatchJob.status.in_(
                    [
                        DispatchJobStatus.PENDING,
                        DispatchJobStatus.RETRY,
                    ]
                ),
            )
            .values(
                status=DispatchJobStatus.RETRY,
                retry_count=DispatchJob.retry_count + 1,
                available_at=next_attempt,
                locked_at=None,
                locked_by=None,
                last_error=error[:2000],
            )
        )

        self.session.execute(query)

    def get_for_update(
        self,
        job_id: UUID,
    ) -> DispatchJob | None:
        query = select(DispatchJob).where(DispatchJob.id == job_id).with_for_update()

        return self.session.scalar(query)

    def mark_dispatching(self, job: DispatchJob, *, worker_id: str) -> None:
        now = datetime.now(UTC)
        job.status = DispatchJobStatus.DISPATCHING
        job.locked_at = now
        job.locked_by = worker_id
        job.last_error = None

    def mark_completed(
        self,
        job: DispatchJob,
        *,
        provider_message_id: str,
    ) -> None:
        now = datetime.now(UTC)

        job.status = DispatchJobStatus.COMPLETED
        job.provider_message_id = provider_message_id
        job.completed_at = now
        job.locked_at = None
        job.locked_by = None
        job.last_error = None

    def mark_delivery_retry(
        self,
        job: DispatchJob,
        *,
        error: str,
        delay_seconds: float,
    ) -> None:
        now = datetime.now(UTC)

        job.status = DispatchJobStatus.RETRY
        job.delivery_attempts += 1
        job.available_at = now + timedelta(seconds=delay_seconds)
        job.locked_at = None
        job.locked_by = None
        job.last_error = error[:2000]

    def mark_failed(
        self,
        job: DispatchJob,
        *,
        error: str,
    ) -> None:
        now = datetime.now(UTC)

        job.status = DispatchJobStatus.FAILED
        job.completed_at = now
        job.locked_at = None
        job.locked_by = None
        job.last_error = error[:2000]

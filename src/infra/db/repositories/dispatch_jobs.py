from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import Select, or_, select, update
from sqlalchemy.orm import Session

from domain.enums import DispatchJobStatus, MessagePriority
from infra.db.models.dispatch_job import DispatchJob


class DispatchJobRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, message_id: UUID, priority: MessagePriority, payload: dict[str, Any]):
        job_payload = {
            "message_id": str(message_id),
            "user_id": payload["user_id"],
            "recipient": payload["recipient"],
            "body": payload["body"],
            "priority": priority.value,
            "cost": str(payload["cost"]),
        }
        job = DispatchJob(message_id=message_id, priority=priority, payload=job_payload)
        self.session.add(job)
        return job

    def claim_batch(
        self,
        *,
        priority: MessagePriority,
        relay_id: str,
        limit: int,
        lease_seconds: int,
    ) -> list[DispatchJob]:
        now = datetime.now(UTC)
        stale_before = now - timedelta(seconds=lease_seconds)

        query = self._build_claim_batch_query(
            priority=priority,
            now=now,
            stale_before=stale_before,
            limit=limit,
        )

        jobs = list(self.session.scalars(query).all())

        for job in jobs:
            job.locked_at = now
            job.locked_by = relay_id

        return jobs

    @staticmethod
    def _build_claim_batch_query(
        *,
        priority: MessagePriority,
        now: datetime,
        stale_before: datetime,
        limit: int,
    ) -> Select[tuple[DispatchJob]]:
        return (
            select(DispatchJob)
            .where(
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
            .order_by(
                DispatchJob.available_at,
                DispatchJob.created_at,
            )
            .with_for_update(skip_locked=True)
            .limit(limit)
        )

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
        job.retry_count += 1
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

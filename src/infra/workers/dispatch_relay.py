import os
import random
import socket
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from core.logging import configure_logging, get_logger
from domain.enums import MessagePriority
from infra.db.repositories.dispatch_jobs import DispatchJobRepository
from infra.db.session import SessionLocal
from infra.workers.dispatch_tasks import process_dispatch_job

logger = get_logger(__name__)

IDLE_SLEEP_SECONDS = 0.5
LEASE_SECONDS = 60

EXPRESS_BATCH_SIZE = 20
NORMAL_BATCH_SIZE = 80


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    retry_count: int


def create_relay_id() -> str:
    return f"{socket.gethostname()}:" f"{os.getpid()}:" f"{uuid4().hex[:8]}"


def queue_for_priority(priority: MessagePriority) -> str:
    if priority == MessagePriority.EXPRESS:
        return "sms.express"

    return "sms.normal"


def calculate_backoff(retry_count: int) -> float:
    exponential_delay = min(60, 2 ** min(retry_count, 6))
    jitter = random.uniform(0, 1)
    return exponential_delay + jitter


def claim_jobs(
    *,
    priority: MessagePriority,
    relay_id: str,
    limit: int,
) -> list[ClaimedJob]:
    logger.debug(
        "dispatch_relay_claim_started",
        extra={
            "relay_id": relay_id,
            "priority": priority.value,
            "limit": limit,
            "lease_seconds": LEASE_SECONDS,
        },
    )

    with SessionLocal() as session, session.begin():
        repository = DispatchJobRepository(session)

        jobs = repository.claim_batch(
            priority=priority,
            relay_id=relay_id,
            limit=limit,
            lease_seconds=LEASE_SECONDS,
        )

        claimed_jobs = [ClaimedJob(id=job.id, retry_count=job.retry_count) for job in jobs]

        logger.debug(
            "dispatch_relay_claim_finished",
            extra={
                "relay_id": relay_id,
                "priority": priority.value,
                "limit": limit,
                "claimed_count": len(claimed_jobs),
            },
        )

        return claimed_jobs


def publish_claimed_jobs(
    *,
    jobs: list[ClaimedJob],
    priority: MessagePriority,
    relay_id: str,
) -> None:
    if not jobs:
        logger.debug(
            "dispatch_relay_publish_skipped",
            extra={
                "relay_id": relay_id,
                "priority": priority.value,
                "job_count": 0,
            },
        )
        return

    queue = queue_for_priority(priority)

    logger.debug(
        "dispatch_relay_publish_started",
        extra={
            "relay_id": relay_id,
            "priority": priority.value,
            "queue": queue,
            "job_count": len(jobs),
        },
    )

    published_ids: list[UUID] = []
    failed_jobs: list[ClaimedJob] = []
    publish_error: Exception | None = None

    for index, job in enumerate(jobs):
        try:
            process_dispatch_job.apply_async(
                args=[str(job.id)],
                task_id=str(job.id),
                queue=queue,
                routing_key=queue,
            )
            published_ids.append(job.id)
            logger.debug(
                "dispatch_job_published_to_broker",
                extra={
                    "dispatch_job_id": str(job.id),
                    "relay_id": relay_id,
                    "priority": priority.value,
                    "queue": queue,
                    "published_count": len(published_ids),
                },
            )
        except Exception as exc:
            publish_error = exc
            failed_jobs.extend(jobs[index:])
            logger.warning(
                "dispatch_publication_interrupted",
                extra={
                    "dispatch_job_id": str(job.id),
                    "relay_id": relay_id,
                    "priority": priority.value,
                    "queue": queue,
                    "published_count": len(published_ids),
                    "remaining_jobs": len(failed_jobs),
                    "error": repr(exc),
                },
                exc_info=True,
            )
            break

    with SessionLocal() as session, session.begin():
        repository = DispatchJobRepository(session)
        logger.debug(
            "dispatch_relay_mark_published_started",
            extra={
                "relay_id": relay_id,
                "priority": priority.value,
                "queue": queue,
                "published_count": len(published_ids),
            },
        )
        repository.mark_published(
            job_ids=published_ids,
            relay_id=relay_id,
        )

        if failed_jobs:
            highest_retry_count = max(job.retry_count for job in failed_jobs)
            delay_seconds = calculate_backoff(highest_retry_count)

            repository.mark_publish_retry(
                job_ids=[job.id for job in failed_jobs],
                relay_id=relay_id,
                error=repr(publish_error),
                delay_seconds=delay_seconds,
            )
            logger.debug(
                "dispatch_relay_publish_retry_marked",
                extra={
                    "relay_id": relay_id,
                    "priority": priority.value,
                    "queue": queue,
                    "failed_count": len(failed_jobs),
                    "highest_retry_count": highest_retry_count,
                    "delay_seconds": delay_seconds,
                    "error": repr(publish_error),
                },
            )

    logger.info(
        "dispatch_relay_publish_batch_finished",
        extra={
            "relay_id": relay_id,
            "priority": priority.value,
            "queue": queue,
            "job_count": len(jobs),
            "published_count": len(published_ids),
            "failed_count": len(failed_jobs),
        },
    )


def run() -> None:
    configure_logging()
    relay_id = create_relay_id()

    logger.info(
        "dispatch_relay_started",
        extra={
            "relay_id": relay_id,
            "lease_seconds": LEASE_SECONDS,
            "idle_sleep_seconds": IDLE_SLEEP_SECONDS,
            "express_batch_size": EXPRESS_BATCH_SIZE,
            "normal_batch_size": NORMAL_BATCH_SIZE,
        },
    )

    while True:
        found_work = False

        express_jobs = claim_jobs(
            priority=MessagePriority.EXPRESS,
            relay_id=relay_id,
            limit=EXPRESS_BATCH_SIZE,
        )

        if express_jobs:
            logger.info(
                "dispatch_relay_jobs_claimed",
                extra={
                    "relay_id": relay_id,
                    "priority": MessagePriority.EXPRESS.value,
                    "claimed_count": len(express_jobs),
                    "limit": EXPRESS_BATCH_SIZE,
                },
            )

            found_work = True
            publish_claimed_jobs(
                jobs=express_jobs,
                priority=MessagePriority.EXPRESS,
                relay_id=relay_id,
            )

        normal_jobs = claim_jobs(
            priority=MessagePriority.NORMAL,
            relay_id=relay_id,
            limit=NORMAL_BATCH_SIZE,
        )

        if normal_jobs:
            logger.info(
                "dispatch_relay_jobs_claimed",
                extra={
                    "relay_id": relay_id,
                    "priority": MessagePriority.NORMAL.value,
                    "claimed_count": len(normal_jobs),
                    "limit": NORMAL_BATCH_SIZE,
                },
            )

            found_work = True
            publish_claimed_jobs(
                jobs=normal_jobs,
                priority=MessagePriority.NORMAL,
                relay_id=relay_id,
            )

        if not found_work:
            logger.debug(
                "dispatch_relay_idle",
                extra={
                    "relay_id": relay_id,
                    "idle_sleep_seconds": IDLE_SLEEP_SECONDS,
                },
            )
            time.sleep(IDLE_SLEEP_SECONDS)


if __name__ == "__main__":
    run()

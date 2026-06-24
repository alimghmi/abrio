import argparse
import os
import random
import socket
import time
from dataclasses import dataclass
from uuid import UUID, uuid4

from prometheus_client import start_http_server

from backend_pre_start import wait_for_database
from core.config import get_settings
from core.logging import configure_logging, get_logger
from core.metrics import (
    REGISTRY,
    record_dispatch_retry,
    record_relay_publish,
    record_relay_publish_failure,
)
from domain.enums import MessagePriority
from infra.db.repositories.dispatch_jobs import DispatchJobRepository
from infra.db.session import SessionLocal
from infra.workers.dispatch_tasks import process_dispatch_job

logger = get_logger(__name__)
IDLE_SLEEP_SECONDS = 0.5

# How long a claim lease is acknowledged before another replica may steal the job.
LEASE_SECONDS = 60

# How long a job may sit in-flight (PUBLISHED awaiting a worker, or DISPATCHING
# while a worker delivers) before the reaper assumes it was lost and returns it
# to RETRY.
INFLIGHT_LEASE_SECONDS = 120

# Throttle for the reaper sweep so it doesn't run every loop iteration.
REAP_INTERVAL_SECONDS = 30
REAP_LIMIT = 200
METRICS_PORT = 9101


@dataclass(frozen=True)
class PriorityConfig:
    priority: MessagePriority
    queue: str
    batch_size: int
    per_user_limit: int


def _build_priority_configs() -> dict[MessagePriority, PriorityConfig]:
    s = get_settings()
    return {
        MessagePriority.EXPRESS: PriorityConfig(
            priority=MessagePriority.EXPRESS,
            queue="sms.express",
            batch_size=s.relay_express_batch_size,
            per_user_limit=s.relay_express_per_user_limit,
        ),
        MessagePriority.NORMAL: PriorityConfig(
            priority=MessagePriority.NORMAL,
            queue="sms.normal",
            batch_size=s.relay_normal_batch_size,
            per_user_limit=s.relay_normal_per_user_limit,
        ),
    }


PRIORITY_CONFIGS: dict[MessagePriority, PriorityConfig] = _build_priority_configs()


@dataclass(frozen=True)
class ClaimedJob:
    id: UUID
    retry_count: int


def create_relay_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex[:8]}"


def calculate_backoff(retry_count: int) -> float:
    exponential_delay = min(60, 2 ** min(retry_count, 6))
    jitter = random.uniform(0, 1)
    return exponential_delay + jitter


def reclaim_stale_inflight(*, config: PriorityConfig, relay_id: str) -> int:
    with SessionLocal() as session, session.begin():
        repository = DispatchJobRepository(session)
        reclaimed = repository.reclaim_stale_inflight(
            priority=config.priority,
            lease_seconds=INFLIGHT_LEASE_SECONDS,
            limit=REAP_LIMIT,
        )

    if reclaimed:
        record_dispatch_retry(
            priority=config.priority,
            retry_type="delivery",
            count=reclaimed,
        )
        logger.warning(
            "dispatch_retry_scheduled",
            extra={
                "relay_id": relay_id,
                "priority": config.priority.value,
                "retry_type": "delivery",
                "reclaimed_count": reclaimed,
            },
        )
        logger.warning(
            "dispatch_relay_reclaimed_stale_inflight",
            extra={
                "relay_id": relay_id,
                "priority": config.priority.value,
                "reclaimed_count": reclaimed,
                "inflight_lease_seconds": INFLIGHT_LEASE_SECONDS,
            },
        )

    return reclaimed


def claim_jobs(*, config: PriorityConfig, relay_id: str) -> list[ClaimedJob]:
    logger.debug(
        "dispatch_relay_claim_started",
        extra={
            "relay_id": relay_id,
            "priority": config.priority.value,
            "limit": config.batch_size,
            "per_user_limit": config.per_user_limit,
            "lease_seconds": LEASE_SECONDS,
        },
    )

    with SessionLocal() as session, session.begin():
        repository = DispatchJobRepository(session)

        jobs = repository.claim_batch(
            priority=config.priority,
            relay_id=relay_id,
            limit=config.batch_size,
            lease_seconds=LEASE_SECONDS,
            per_user_limit=config.per_user_limit,
        )

        claimed_jobs = [ClaimedJob(id=job.id, retry_count=job.retry_count) for job in jobs]

        logger.debug(
            "dispatch_relay_claim_finished",
            extra={
                "relay_id": relay_id,
                "priority": config.priority.value,
                "limit": config.batch_size,
                "claimed_count": len(claimed_jobs),
            },
        )

        return claimed_jobs


def publish_claimed_jobs(
    *,
    jobs: list[ClaimedJob],
    config: PriorityConfig,
    relay_id: str,
) -> None:
    if not jobs:
        return

    queue = config.queue

    logger.debug(
        "dispatch_relay_publish_started",
        extra={
            "relay_id": relay_id,
            "priority": config.priority.value,
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
                    "priority": config.priority.value,
                    "queue": queue,
                    "published_count": len(published_ids),
                },
            )
        except Exception as exc:
            publish_error = exc
            failed_jobs.extend(jobs[index:])
            logger.warning(
                "dispatch_publish_failed",
                extra={
                    "dispatch_job_id": str(job.id),
                    "relay_id": relay_id,
                    "priority": config.priority.value,
                    "queue": queue,
                    "published_count": len(published_ids),
                    "remaining_jobs": len(failed_jobs),
                    "error_type": type(exc).__name__,
                },
                exc_info=True,
            )
            break

    with SessionLocal() as session, session.begin():
        repository = DispatchJobRepository(session)
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
            logger.warning(
                "dispatch_retry_scheduled",
                extra={
                    "relay_id": relay_id,
                    "priority": config.priority.value,
                    "queue": queue,
                    "failed_count": len(failed_jobs),
                    "highest_retry_count": highest_retry_count,
                    "delay_seconds": delay_seconds,
                    "error_type": type(publish_error).__name__ if publish_error else None,
                },
            )

    record_relay_publish(priority=config.priority, count=len(published_ids))
    record_relay_publish_failure(priority=config.priority, count=len(failed_jobs))
    record_dispatch_retry(
        priority=config.priority,
        retry_type="publication",
        count=len(failed_jobs),
    )
    logger.info(
        "dispatch_jobs_published",
        extra={
            "relay_id": relay_id,
            "priority": config.priority.value,
            "queue": queue,
            "job_count": len(jobs),
            "published_count": len(published_ids),
            "failed_count": len(failed_jobs),
        },
    )


def run(priority: MessagePriority) -> None:
    configure_logging()
    wait_for_database()
    metrics_port = int(os.environ.get("METRICS_PORT", str(METRICS_PORT)))
    start_http_server(metrics_port, addr="0.0.0.0", registry=REGISTRY)
    relay_id = create_relay_id()
    config = PRIORITY_CONFIGS[priority]

    logger.info(
        "dispatch_relay_started",
        extra={
            "relay_id": relay_id,
            "priority": config.priority.value,
            "queue": config.queue,
            "batch_size": config.batch_size,
            "per_user_limit": config.per_user_limit,
            "lease_seconds": LEASE_SECONDS,
            "idle_sleep_seconds": IDLE_SLEEP_SECONDS,
        },
    )

    last_reap = time.monotonic() - REAP_INTERVAL_SECONDS

    while True:
        now = time.monotonic()
        if now - last_reap >= REAP_INTERVAL_SECONDS:
            reclaim_stale_inflight(config=config, relay_id=relay_id)
            last_reap = now

        jobs = claim_jobs(config=config, relay_id=relay_id)

        if jobs:
            logger.info(
                "dispatch_jobs_claimed",
                extra={
                    "relay_id": relay_id,
                    "priority": config.priority.value,
                    "claimed_count": len(jobs),
                    "limit": config.batch_size,
                },
            )
            publish_claimed_jobs(jobs=jobs, config=config, relay_id=relay_id)
        else:
            logger.debug(
                "dispatch_relay_idle",
                extra={
                    "relay_id": relay_id,
                    "priority": config.priority.value,
                    "idle_sleep_seconds": IDLE_SLEEP_SECONDS,
                },
            )
            time.sleep(IDLE_SLEEP_SECONDS)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SMS dispatch relay")
    parser.add_argument(
        "-Q",
        "--queue",
        choices=["normal", "express"],
        default="normal",
        help="Which priority this relay claims and publishes (default: normal)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    priority = MessagePriority.EXPRESS if args.queue == "express" else MessagePriority.NORMAL
    run(priority)


if __name__ == "__main__":
    main()

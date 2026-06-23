from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Final

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
    multiprocess,
)
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from core.logging import get_logger
from domain.enums import DispatchJobStatus, MessagePriority, PaymentStatus
from infra.db.models.balance import Balance
from infra.db.models.dispatch_job import DispatchJob
from infra.db.models.message import Message
from infra.db.session import SessionLocal

REGISTRY = CollectorRegistry(auto_describe=True)

PRIORITIES: Final = frozenset(priority.value for priority in MessagePriority)
SUBMISSION_TYPES: Final = frozenset({"single", "batch"})
OUTCOMES: Final = frozenset(
    {"success", "temporary_failure", "permanent_failure", "unexpected_failure"}
)
RETRY_TYPES: Final = frozenset({"publication", "delivery"})
REJECTION_REASONS: Final = frozenset(
    {
        "rate_limited",
        "global_rate_limited",
        "system_rate_limited",
        "insufficient_balance",
        "invalid_request",
        "idempotency_conflict",
        "database_error",
    }
)
PAYMENT_VIOLATION_TYPES: Final = frozenset(
    {
        "negative_credits",
        "negative_reserved_credits",
        "reserved_exceeds_credits",
        "reserved_without_message",
        "message_balance_mismatch",
    }
)
DB_METRIC_CACHE_SECONDS: Final = 5.0

HTTP_REQUESTS = Counter(
    "abrio_http_requests_total",
    "HTTP requests handled by the API.",
    ("method", "route", "status_code"),
    registry=REGISTRY,
)
HTTP_REQUEST_DURATION = Histogram(
    "abrio_http_request_duration_seconds",
    "HTTP request duration in seconds.",
    ("method", "route"),
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)
MESSAGES_SUBMITTED = Counter(
    "abrio_messages_submitted_total",
    "Accepted messages after the submission transaction commits.",
    ("priority", "submission_type"),
    registry=REGISTRY,
)
MESSAGE_SUBMISSION_REJECTED = Counter(
    "abrio_message_submission_rejected_total",
    "Rejected message submissions.",
    ("reason", "submission_type"),
    registry=REGISTRY,
)
IDEMPOTENT_REPLAYS = Counter(
    "abrio_idempotent_replays_total",
    "Idempotent message submission replays.",
    ("submission_type",),
    registry=REGISTRY,
)
DISPATCH_READY_JOBS = Gauge(
    "abrio_dispatch_ready_jobs",
    "Dispatch jobs that are currently eligible for relay publication.",
    ("priority",),
    registry=REGISTRY,
)
DISPATCH_OLDEST_READY_AGE = Gauge(
    "abrio_dispatch_oldest_ready_age_seconds",
    "Age in seconds of the oldest dispatch job eligible for publication.",
    ("priority",),
    registry=REGISTRY,
)
RELAY_JOBS_PUBLISHED = Counter(
    "abrio_relay_jobs_published_total",
    "Dispatch jobs published to RabbitMQ by the relay.",
    ("priority",),
    registry=REGISTRY,
)
RELAY_PUBLISH_FAILURES = Counter(
    "abrio_relay_publish_failures_total",
    "Dispatch jobs the relay failed to publish to RabbitMQ.",
    ("priority",),
    registry=REGISTRY,
)
DELIVERY_ATTEMPTS = Counter(
    "abrio_delivery_attempts_total",
    "Committed delivery outcomes.",
    ("priority", "outcome"),
    registry=REGISTRY,
)
MESSAGE_END_TO_END_DURATION = Histogram(
    "abrio_message_end_to_end_duration_seconds",
    "Message duration from submission to committed delivery outcome.",
    ("priority", "outcome"),
    buckets=(0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0),
    registry=REGISTRY,
)
DISPATCH_RETRIES = Counter(
    "abrio_dispatch_retries_total",
    "Dispatch retries scheduled by retry type.",
    ("priority", "retry_type"),
    registry=REGISTRY,
)
PAYMENT_CONSISTENCY_VIOLATIONS = Gauge(
    "abrio_payment_consistency_violations",
    "Payment consistency violations calculated from database state.",
    ("type",),
    registry=REGISTRY,
)

logger = get_logger(__name__)


@dataclass
class _DatabaseMetricCache:
    expires_at: float = 0.0
    had_success: bool = False


_db_cache = _DatabaseMetricCache()


def record_http_request(
    *,
    method: str,
    route: str,
    status_code: int,
    duration_seconds: float,
) -> None:
    HTTP_REQUESTS.labels(
        method=method.upper(),
        route=route,
        status_code=str(status_code),
    ).inc()
    HTTP_REQUEST_DURATION.labels(method=method.upper(), route=route).observe(duration_seconds)


def record_messages_submitted(
    *,
    priority: MessagePriority | str,
    submission_type: str,
    count: int = 1,
) -> None:
    if count <= 0:
        return

    MESSAGES_SUBMITTED.labels(
        priority=_validated(priority, PRIORITIES, "priority"),
        submission_type=_validated(submission_type, SUBMISSION_TYPES, "submission_type"),
    ).inc(count)


def record_submission_rejected(*, reason: str, submission_type: str) -> None:
    MESSAGE_SUBMISSION_REJECTED.labels(
        reason=_validated(reason, REJECTION_REASONS, "reason"),
        submission_type=_validated(submission_type, SUBMISSION_TYPES, "submission_type"),
    ).inc()


def record_idempotent_replay(*, submission_type: str) -> None:
    IDEMPOTENT_REPLAYS.labels(
        submission_type=_validated(submission_type, SUBMISSION_TYPES, "submission_type")
    ).inc()


def record_relay_publish(*, priority: MessagePriority | str, count: int) -> None:
    if count <= 0:
        return

    RELAY_JOBS_PUBLISHED.labels(priority=_validated(priority, PRIORITIES, "priority")).inc(count)


def record_relay_publish_failure(*, priority: MessagePriority | str, count: int) -> None:
    if count <= 0:
        return

    RELAY_PUBLISH_FAILURES.labels(priority=_validated(priority, PRIORITIES, "priority")).inc(count)


def record_dispatch_retry(
    *,
    priority: MessagePriority | str,
    retry_type: str,
    count: int = 1,
) -> None:
    if count <= 0:
        return

    DISPATCH_RETRIES.labels(
        priority=_validated(priority, PRIORITIES, "priority"),
        retry_type=_validated(retry_type, RETRY_TYPES, "retry_type"),
    ).inc(count)


def record_delivery_outcome(
    *,
    priority: MessagePriority | str,
    outcome: str,
    message_created_at: datetime,
) -> None:
    priority_label = _validated(priority, PRIORITIES, "priority")
    outcome_label = _validated(outcome, OUTCOMES, "outcome")
    DELIVERY_ATTEMPTS.labels(priority=priority_label, outcome=outcome_label).inc()

    now = datetime.now(UTC)
    created_at = message_created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    duration_seconds = max(0.0, (now - created_at).total_seconds())
    MESSAGE_END_TO_END_DURATION.labels(
        priority=priority_label,
        outcome=outcome_label,
    ).observe(duration_seconds)


def collect_database_metrics(*, force: bool = False) -> None:
    now = time.monotonic()
    if not force and _db_cache.had_success and now < _db_cache.expires_at:
        return

    try:
        with SessionLocal() as session:
            _collect_dispatch_ready_metrics(session)
            _collect_payment_consistency_metrics(session)
    except Exception as exc:
        logger.error(
            "dependency_unavailable",
            extra={
                "dependency": "database",
                "operation": "metrics_collection",
                "error_type": type(exc).__name__,
            },
            exc_info=True,
        )
        return

    _db_cache.had_success = True
    _db_cache.expires_at = now + DB_METRIC_CACHE_SECONDS


def latest_metrics() -> bytes:
    collect_database_metrics()
    return generate_latest(REGISTRY)


def latest_multiprocess_metrics() -> bytes:
    registry = CollectorRegistry()
    multiprocess.MultiProcessCollector(registry)
    return generate_latest(registry)


def reset_database_metric_cache() -> None:
    _db_cache.expires_at = 0.0
    _db_cache.had_success = False


def reset_metrics_for_tests() -> None:
    for metric in (
        HTTP_REQUESTS,
        HTTP_REQUEST_DURATION,
        MESSAGES_SUBMITTED,
        MESSAGE_SUBMISSION_REJECTED,
        IDEMPOTENT_REPLAYS,
        DISPATCH_READY_JOBS,
        DISPATCH_OLDEST_READY_AGE,
        RELAY_JOBS_PUBLISHED,
        RELAY_PUBLISH_FAILURES,
        DELIVERY_ATTEMPTS,
        MESSAGE_END_TO_END_DURATION,
        DISPATCH_RETRIES,
        PAYMENT_CONSISTENCY_VIOLATIONS,
    ):
        metric._metrics.clear()  # type: ignore[attr-defined]
    reset_database_metric_cache()


def _collect_dispatch_ready_metrics(session: Session) -> None:
    now = datetime.now(UTC)
    stale_before = now - timedelta(seconds=60)
    ready_query = (
        select(
            DispatchJob.priority,
            func.count(DispatchJob.id).label("ready_count"),
            func.min(DispatchJob.created_at).label("oldest_created_at"),
        )
        .where(
            DispatchJob.status.in_([DispatchJobStatus.PENDING, DispatchJobStatus.RETRY]),
            DispatchJob.available_at <= now,
            or_(
                DispatchJob.locked_at.is_(None),
                DispatchJob.locked_at < stale_before,
            ),
        )
        .group_by(DispatchJob.priority)
    )
    values = {priority: (0, 0.0) for priority in PRIORITIES}
    for row in session.execute(ready_query):
        priority = row.priority.value if isinstance(row.priority, MessagePriority) else row.priority
        oldest_created_at = row.oldest_created_at
        oldest_age_seconds = 0.0
        if oldest_created_at is not None:
            if oldest_created_at.tzinfo is None:
                oldest_created_at = oldest_created_at.replace(tzinfo=UTC)
            oldest_age_seconds = max(0.0, (now - oldest_created_at).total_seconds())
        values[priority] = (int(row.ready_count or 0), oldest_age_seconds)

    for priority, (ready_count, oldest_age_seconds) in values.items():
        DISPATCH_READY_JOBS.labels(priority=priority).set(ready_count)
        DISPATCH_OLDEST_READY_AGE.labels(priority=priority).set(oldest_age_seconds)


def _collect_payment_consistency_metrics(session: Session) -> None:
    payment_query = select(
        func.count(Balance.id).filter(Balance.credits < Decimal("0.00")).label("negative_credits"),
        func.count(Balance.id)
        .filter(Balance.reserved_credits < Decimal("0.00"))
        .label("negative_reserved_credits"),
        func.count(Balance.id)
        .filter(Balance.reserved_credits > Balance.credits)
        .label("reserved_exceeds_credits"),
    )
    row = session.execute(payment_query).one()

    reserved_messages = (
        select(
            Message.user_id.label("user_id"),
            func.coalesce(func.sum(Message.cost), Decimal("0.00")).label("reserved_total"),
        )
        .where(Message.payment_status == PaymentStatus.RESERVED)
        .group_by(Message.user_id)
        .subquery()
    )
    mismatch_query = (
        select(
            func.count(Balance.id)
            .filter(
                and_(
                    Balance.reserved_credits > Decimal("0.00"),
                    func.coalesce(reserved_messages.c.reserved_total, Decimal("0.00"))
                    == Decimal("0.00"),
                )
            )
            .label("reserved_without_message"),
            func.count(Balance.id)
            .filter(
                Balance.reserved_credits
                != func.coalesce(reserved_messages.c.reserved_total, Decimal("0.00"))
            )
            .label("message_balance_mismatch"),
        )
        .select_from(Balance)
        .outerjoin(reserved_messages, reserved_messages.c.user_id == Balance.user_id)
    )
    mismatch_row = session.execute(mismatch_query).one()

    values = {
        "negative_credits": int(row.negative_credits or 0),
        "negative_reserved_credits": int(row.negative_reserved_credits or 0),
        "reserved_exceeds_credits": int(row.reserved_exceeds_credits or 0),
        "reserved_without_message": int(mismatch_row.reserved_without_message or 0),
        "message_balance_mismatch": int(mismatch_row.message_balance_mismatch or 0),
    }

    for violation_type in PAYMENT_VIOLATION_TYPES:
        count = values.get(violation_type, 0)
        PAYMENT_CONSISTENCY_VIOLATIONS.labels(type=violation_type).set(count)
        if count:
            logger.warning(
                "payment_consistency_violation",
                extra={
                    "type": violation_type,
                    "count": count,
                },
            )


def _validated(value: MessagePriority | str, allowed: frozenset[str], label_name: str) -> str:
    label = value.value if isinstance(value, MessagePriority) else str(value)
    if label not in allowed:
        raise ValueError(f"Invalid {label_name} metric label: {label}")
    return label

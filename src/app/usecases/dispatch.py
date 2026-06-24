import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from core.logging import get_logger
from core.metrics import record_delivery_outcome, record_dispatch_retry
from domain.enums import (
    DispatchJobStatus,
    MessagePriority,
    PaymentStatus,
)
from infra.db.repositories.balance import BalanceRepositry
from infra.db.repositories.dispatch_jobs import DispatchJobRepository
from infra.db.repositories.messages import MessageRepository
from infra.providers.types import (
    ProviderOutcome,
    ProviderResult,
    SmsProvider,
)

TERMINAL_JOB_STATUSES = {
    DispatchJobStatus.COMPLETED,
    DispatchJobStatus.FAILED,
}

# How long a worker's delivery lease is honoured before the reaper assumes the
# worker died mid-delivery and returns the job to RETRY.
WORKER_LEASE_SECONDS = 120

logger = get_logger(__name__)


@dataclass(frozen=True)
class _DeliveryContext:
    message_id: UUID
    recipient: str
    body: str
    priority: MessagePriority


@dataclass(frozen=True)
class _CommittedDeliveryMetric:
    priority: MessagePriority
    outcome: str
    message_created_at: datetime


class DispatchUseCase:
    def __init__(
        self,
        session: Session,
        provider: SmsProvider,
        *,
        max_delivery_attempts: int = 5,
        express_ttl_seconds: int | None = None,
        worker_lease_seconds: int = WORKER_LEASE_SECONDS,
    ):
        self.session = session
        self.provider = provider
        self.max_delivery_attempts = max_delivery_attempts
        self.express_ttl_seconds = express_ttl_seconds
        self.worker_lease_seconds = worker_lease_seconds
        self._job_repo = DispatchJobRepository(session)
        self._message_repo = MessageRepository(session)
        self._balance_repo = BalanceRepositry(session)

    def process(self, job_id: UUID) -> None:
        worker_id = uuid4().hex

        logger.debug(
            "dispatch_usecase_processing_started",
            extra={
                "dispatch_job_id": str(job_id),
                "worker_id": worker_id,
                "max_delivery_attempts": self.max_delivery_attempts,
            },
        )

        delivery = self._claim_for_delivery(job_id, worker_id)
        if delivery is None:
            return

        logger.info(
            "delivery_attempt_started",
            extra={
                "dispatch_job_id": str(job_id),
                "message_id": str(delivery.message_id),
                "priority": delivery.priority.value,
            },
        )
        result = self.provider.send(
            message_id=delivery.message_id,
            recipient=delivery.recipient,
            body=delivery.body,
        )
        logger.debug(
            "dispatch_provider_response_received",
            extra={
                "dispatch_job_id": str(job_id),
                "message_id": str(delivery.message_id),
                "provider_outcome": result.outcome.value,
            },
        )

        self._record_outcome(job_id, worker_id, result)

    def _claim_for_delivery(self, job_id: UUID, worker_id: str) -> _DeliveryContext | None:
        """P1: validate the job and claim it for delivery by
        moving it to DISPATCHING with a worker lease. Returns the snapshot the
        provider call needs, or None if there is nothing to deliver (duplicate,
        already terminal, not processable, or finalised here as expired)."""
        expired_metric: _CommittedDeliveryMetric | None = None
        expired_log_extra: dict[str, object] | None = None
        delivery_context: _DeliveryContext | None = None

        with self.session.begin():
            job = self._job_repo.get_for_update(job_id)

            if job is None:
                logger.debug("dispatch_job_not_found", extra={"dispatch_job_id": str(job_id)})
                return None

            if job.status in TERMINAL_JOB_STATUSES:
                logger.debug(
                    "dispatch_job_duplicate_delivery_ignored",
                    extra={"dispatch_job_id": str(job.id), "status": job.status.value},
                )
                return None

            if not self._is_claimable(job, worker_id):
                return None

            message = self._message_repo.get_for_update(job.message_id)

            if message.payment_status != PaymentStatus.RESERVED:
                logger.error(
                    "dispatch_job_payment_not_reserved",
                    extra={
                        "dispatch_job_id": str(job.id),
                        "message_id": str(message.id),
                        "status": job.status.value,
                        "payment_status": message.payment_status.value,
                    },
                )
                raise RuntimeError(
                    "Dispatch job payment is not reserved: "
                    f"job_id={job.id}, "
                    f"message_id={message.id}, "
                    f"payment_status={message.payment_status}"
                )

            if self._express_deadline_exceeded(message):
                self._permanently_fail(
                    job=job, message=message, error="Express delivery deadline exceeded"
                )
                expired_metric = _CommittedDeliveryMetric(
                    priority=message.priority,
                    outcome="permanent_failure",
                    message_created_at=message.created_at,
                )
                expired_log_extra = {
                    "dispatch_job_id": str(job.id),
                    "message_id": str(message.id),
                    "priority": message.priority.value,
                    "outcome": "permanent_failure",
                    "delivery_attempts": job.delivery_attempts,
                    "express_ttl_seconds": self.express_ttl_seconds,
                    "error_type": "express_deadline_exceeded",
                }
            else:
                self._message_repo.mark_dispatching(message)
                self._job_repo.mark_dispatching(job, worker_id=worker_id)

                delivery_context = _DeliveryContext(
                    message_id=message.id,
                    recipient=message.recipient,
                    body=message.body,
                    priority=message.priority,
                )

        if expired_metric is not None:
            _record_committed_delivery_metric(expired_metric)
            logger.warning("delivery_attempt_permanently_failed", extra=expired_log_extra or {})

        return delivery_context

    def _is_claimable(self, job, worker_id: str) -> bool:
        now = datetime.now(UTC)
        if job.status == DispatchJobStatus.DISPATCHING:
            lease_fresh = job.locked_at is not None and job.locked_at > now - timedelta(
                seconds=self.worker_lease_seconds
            )
            if lease_fresh:
                # Another worker is actively delivering this job.
                logger.info(
                    "dispatch_job_in_flight_duplicate_ignored",
                    extra={
                        "dispatch_job_id": str(job.id),
                        "worker_id": worker_id,
                        "locked_by": job.locked_by,
                    },
                )
                return False
            # Stale lease: the previous worker died mid-delivery, reclaim it.
            return True

        publish_in_progress = (
            job.status in {DispatchJobStatus.PENDING, DispatchJobStatus.RETRY}
            and job.locked_at is not None
            and job.locked_by is not None
        )
        if job.status == DispatchJobStatus.PUBLISHED or publish_in_progress:
            return True

        logger.info(
            "dispatch_job_not_processable",
            extra={
                "dispatch_job_id": str(job.id),
                "status": job.status.value,
                "locked_by": job.locked_by,
            },
        )
        return False

    def _record_outcome(self, job_id: UUID, worker_id: str, result: ProviderResult) -> None:
        """P3: persist the provider result, but only if we still
        own the delivery lease taken in phase 1. If a reaper reclaimed the job or
        another worker took over (lease lost), bail without writing so we never
        double-record an outcome."""
        committed_metric: _CommittedDeliveryMetric | None = None
        retry_scheduled = False
        retry_log_extra: dict[str, object] = {}
        log_event: str | None = None
        log_extra: dict[str, object] = {}
        log_level = "info"

        with self.session.begin():
            job = self._job_repo.get_for_update(job_id)

            if (
                job is None
                or job.status != DispatchJobStatus.DISPATCHING
                or (job.locked_by != worker_id)
            ):
                logger.warning(
                    "dispatch_job_delivery_lease_lost",
                    extra={
                        "dispatch_job_id": str(job_id),
                        "worker_id": worker_id,
                        "status": job.status.value if job else None,
                        "locked_by": job.locked_by if job else None,
                    },
                )
                return

            message = self._message_repo.get_for_update(job.message_id)

            if result.outcome == ProviderOutcome.SUCCESS:
                if result.provider_message_id is None:
                    raise RuntimeError("Successful provider result has no provider message ID")

                self._balance_repo.settle(user_id=message.user_id, amount=message.cost)
                self._message_repo.mark_sent(message)
                self._job_repo.mark_completed(job, provider_message_id=result.provider_message_id)
                committed_metric = _CommittedDeliveryMetric(
                    priority=message.priority,
                    outcome="success",
                    message_created_at=message.created_at,
                )
                log_event = "delivery_attempt_succeeded"
                log_extra = {
                    "dispatch_job_id": str(job.id),
                    "message_id": str(message.id),
                    "priority": message.priority.value,
                    "outcome": "success",
                }

            elif result.outcome == ProviderOutcome.PERMANENT_FAILURE:
                error = result.error or "Permanent provider failure"
                self._permanently_fail(job=job, message=message, error=error)
                committed_metric = _CommittedDeliveryMetric(
                    priority=message.priority,
                    outcome="permanent_failure",
                    message_created_at=message.created_at,
                )
                log_event = "delivery_attempt_permanently_failed"
                log_level = "warning"
                log_extra = {
                    "dispatch_job_id": str(job.id),
                    "message_id": str(message.id),
                    "priority": message.priority.value,
                    "outcome": "permanent_failure",
                    "error_type": "provider_permanent_failure",
                }

            else:
                next_attempt = job.delivery_attempts + 1

                if next_attempt >= self.max_delivery_attempts:
                    error = result.error or "Maximum delivery attempts reached"
                    self._permanently_fail(job=job, message=message, error=error)
                    committed_metric = _CommittedDeliveryMetric(
                        priority=message.priority,
                        outcome="permanent_failure",
                        message_created_at=message.created_at,
                    )
                    log_event = "delivery_attempt_permanently_failed"
                    log_level = "warning"
                    log_extra = {
                        "dispatch_job_id": str(job.id),
                        "message_id": str(message.id),
                        "priority": message.priority.value,
                        "outcome": "permanent_failure",
                        "delivery_attempts": next_attempt,
                        "max_delivery_attempts": self.max_delivery_attempts,
                        "error_type": "max_delivery_attempts_reached",
                    }

                else:
                    delay_seconds = self._calculate_backoff(next_attempt)
                    if self._express_deadline_exceeded(message, ahead_seconds=delay_seconds):
                        self._permanently_fail(
                            job=job,
                            message=message,
                            error="Express delivery deadline would be exceeded before next retry",
                        )
                        committed_metric = _CommittedDeliveryMetric(
                            priority=message.priority,
                            outcome="permanent_failure",
                            message_created_at=message.created_at,
                        )
                        log_event = "delivery_attempt_permanently_failed"
                        log_level = "warning"
                        log_extra = {
                            "dispatch_job_id": str(job.id),
                            "message_id": str(message.id),
                            "priority": message.priority.value,
                            "outcome": "permanent_failure",
                            "delivery_attempts": next_attempt,
                            "express_ttl_seconds": self.express_ttl_seconds,
                            "error_type": "express_deadline_exceeded",
                        }

                    else:
                        self._message_repo.mark_retryable_failure(message)
                        self._job_repo.mark_delivery_retry(
                            job,
                            error=result.error or "Temporary provider failure",
                            delay_seconds=delay_seconds,
                        )
                        committed_metric = _CommittedDeliveryMetric(
                            priority=message.priority,
                            outcome="temporary_failure",
                            message_created_at=message.created_at,
                        )
                        retry_scheduled = True
                        retry_log_extra = {
                            "dispatch_job_id": str(job.id),
                            "message_id": str(message.id),
                            "priority": message.priority.value,
                            "retry_type": "delivery",
                        }
                        log_event = "delivery_attempt_temporarily_failed"
                        log_level = "warning"
                        log_extra = {
                            "dispatch_job_id": str(job.id),
                            "message_id": str(message.id),
                            "priority": message.priority.value,
                            "outcome": "temporary_failure",
                            "delivery_attempts": next_attempt,
                            "max_delivery_attempts": self.max_delivery_attempts,
                            "retry_type": "delivery",
                            "delay_seconds": delay_seconds,
                            "error_type": "provider_temporary_failure",
                        }

        if committed_metric is not None:
            _record_committed_delivery_metric(committed_metric)
            if retry_scheduled:
                record_dispatch_retry(
                    priority=committed_metric.priority,
                    retry_type="delivery",
                    count=1,
                )
                logger.warning(
                    "dispatch_retry_scheduled",
                    extra=retry_log_extra,
                )
            if log_event is not None:
                getattr(logger, log_level)(log_event, extra=log_extra)

    def _permanently_fail(
        self,
        *,
        job,
        message,
        error: str,
    ) -> None:
        self._balance_repo.release_credits(
            user_id=message.user_id,
            amount=message.cost,
        )

        self._message_repo.mark_permanent_failure(message)

        self._job_repo.mark_failed(
            job,
            error=error,
        )

    def _express_deadline_exceeded(self, message, *, ahead_seconds: float = 0.0) -> bool:
        if self.express_ttl_seconds is None:
            return False
        if message.priority != MessagePriority.EXPRESS:
            return False

        age_seconds = (datetime.now(UTC) - message.created_at).total_seconds()
        return age_seconds + ahead_seconds > self.express_ttl_seconds

    @staticmethod
    def _calculate_backoff(
        retry_count: int,
    ) -> float:
        base_delay = min(
            60,
            2 ** min(retry_count, 6),
        )
        return base_delay + random.uniform(0, 1)


def _record_committed_delivery_metric(metric: _CommittedDeliveryMetric) -> None:
    record_delivery_outcome(
        priority=metric.priority,
        outcome=metric.outcome,
        message_created_at=metric.message_created_at,
    )

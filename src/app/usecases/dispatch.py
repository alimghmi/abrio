import random
from uuid import UUID

from sqlalchemy.orm import Session

from core.logging import get_logger
from domain.enums import (
    DispatchJobStatus,
    PaymentStatus,
)
from infra.db.repositories.balance import BalanceRepositry
from infra.db.repositories.dispatch_jobs import DispatchJobRepository
from infra.db.repositories.messages import MessageRepository
from infra.providers.types import (
    ProviderOutcome,
    SmsProvider,
)

TERMINAL_JOB_STATUSES = {
    DispatchJobStatus.COMPLETED,
    DispatchJobStatus.FAILED,
}

logger = get_logger(__name__)


class DispatchUseCase:
    def __init__(
        self,
        session: Session,
        provider: SmsProvider,
        *,
        max_delivery_attempts: int = 5,
    ):
        self.session = session
        self.provider = provider
        self.max_delivery_attempts = max_delivery_attempts

        self._job_repo = DispatchJobRepository(session)
        self._message_repo = MessageRepository(session)
        self._balance_repo = BalanceRepositry(session)

    def process(self, job_id: UUID) -> None:
        logger.debug(
            "dispatch_usecase_processing_started",
            extra={
                "dispatch_job_id": str(job_id),
                "max_delivery_attempts": self.max_delivery_attempts,
            },
        )

        with self.session.begin():
            job = self._job_repo.get_for_update(job_id)

            if job is None:
                logger.debug(
                    "dispatch_job_not_found",
                    extra={"dispatch_job_id": str(job_id)},
                )
                return

            if job.status in TERMINAL_JOB_STATUSES:
                # Duplicate Celery delivery after final processing.
                logger.debug(
                    "dispatch_job_duplicate_delivery_ignored",
                    extra={
                        "dispatch_job_id": str(job.id),
                        "status": job.status.value,
                        "retry_count": job.retry_count,
                    },
                )
                return

            publish_in_progress = (
                job.status
                in {
                    DispatchJobStatus.PENDING,
                    DispatchJobStatus.RETRY,
                }
                and job.locked_at is not None
                and job.locked_by is not None
            )

            is_processable = job.status == DispatchJobStatus.PUBLISHED or publish_in_progress

            if not is_processable:
                logger.info(
                    "dispatch_job_not_processable",
                    extra={
                        "dispatch_job_id": str(job.id),
                        "status": job.status.value,
                        "locked_by": job.locked_by,
                        "retry_count": job.retry_count,
                    },
                )
                return

            message = self._message_repo.get_for_update(job.message_id)

            if message.payment_status != PaymentStatus.RESERVED:
                # A non-terminal job with a non-reserved payment is an
                # inconsistent state and should not modify the balance.
                logger.warning(
                    "dispatch_job_payment_not_reserved",
                    extra={
                        "dispatch_job_id": str(job.id),
                        "message_id": str(message.id),
                        "status": job.status.value,
                        "retry_count": job.retry_count,
                        "payment_status": message.payment_status.value,
                    },
                )
                raise RuntimeError(
                    "Dispatch job payment is not reserved: "
                    f"job_id={job.id}, "
                    f"message_id={message.id}, "
                    f"payment_status={message.payment_status}"
                )

            self._message_repo.mark_dispatching(message)

            logger.debug(
                "dispatch_provider_send_started",
                extra={
                    "dispatch_job_id": str(job.id),
                    "message_id": str(message.id),
                    "status": job.status.value,
                    "retry_count": job.retry_count,
                },
            )

            result = self.provider.send(
                message_id=message.id,
                recipient=message.recipient,
                body=message.body,
            )

            logger.debug(
                "dispatch_provider_response_received",
                extra={
                    "dispatch_job_id": str(job.id),
                    "message_id": str(message.id),
                    "status": job.status.value,
                    "retry_count": job.retry_count,
                    "provider_outcome": result.outcome.value,
                    "provider_message_id": result.provider_message_id,
                    "error": result.error,
                },
            )

            if result.outcome == ProviderOutcome.SUCCESS:
                if result.provider_message_id is None:
                    logger.warning(
                        "dispatch_provider_success_missing_message_id",
                        extra={
                            "dispatch_job_id": str(job.id),
                            "message_id": str(message.id),
                            "status": job.status.value,
                            "retry_count": job.retry_count,
                            "provider_outcome": result.outcome.value,
                        },
                    )
                    raise RuntimeError("Successful provider result has no provider message ID")

                self._balance_repo.settle(
                    user_id=message.user_id,
                    amount=message.cost,
                )

                self._message_repo.mark_sent(message)

                self._job_repo.mark_completed(
                    job,
                    provider_message_id=result.provider_message_id,
                )
                logger.info(
                    "dispatch_job_completed",
                    extra={
                        "dispatch_job_id": str(job.id),
                        "message_id": str(message.id),
                        "status": job.status.value,
                        "retry_count": job.retry_count,
                        "provider_outcome": result.outcome.value,
                        "provider_message_id": result.provider_message_id,
                    },
                )
                return

            if result.outcome == ProviderOutcome.PERMANENT_FAILURE:
                error = result.error or "Permanent provider failure"
                self._permanently_fail(
                    job=job,
                    message=message,
                    error=error,
                )
                logger.warning(
                    "dispatch_job_permanently_failed",
                    extra={
                        "dispatch_job_id": str(job.id),
                        "message_id": str(message.id),
                        "status": job.status.value,
                        "retry_count": job.retry_count,
                        "provider_outcome": result.outcome.value,
                        "error": error,
                    },
                )
                return

            next_attempt = job.retry_count + 1

            if next_attempt >= self.max_delivery_attempts:
                error = result.error or "Maximum delivery attempts reached"
                self._permanently_fail(
                    job=job,
                    message=message,
                    error=error,
                )
                logger.warning(
                    "dispatch_job_max_delivery_attempts_reached",
                    extra={
                        "dispatch_job_id": str(job.id),
                        "message_id": str(message.id),
                        "status": job.status.value,
                        "retry_count": next_attempt,
                        "max_delivery_attempts": self.max_delivery_attempts,
                        "provider_outcome": result.outcome.value,
                        "error": error,
                    },
                )
                return

            self._message_repo.mark_retryable_failure(message)

            self._job_repo.mark_delivery_retry(
                job,
                error=result.error or "Temporary provider failure",
                delay_seconds=self._calculate_backoff(next_attempt),
            )

            logger.warning(
                "dispatch_job_scheduled_for_retry",
                extra={
                    "dispatch_job_id": str(job.id),
                    "message_id": str(message.id),
                    "status": job.status.value,
                    "retry_count": next_attempt,
                    "max_delivery_attempts": self.max_delivery_attempts,
                    "provider_outcome": result.outcome.value,
                    "error": result.error,
                },
            )

            return

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

    @staticmethod
    def _calculate_backoff(
        retry_count: int,
    ) -> float:
        base_delay = min(
            60,
            2 ** min(retry_count, 6),
        )

        return base_delay + random.uniform(0, 1)

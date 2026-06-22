from uuid import UUID

from app.usecases.dispatch import DispatchUseCase
from core.logging import get_logger
from infra.db.session import SessionLocal
from infra.providers.dummy import DummySmsProvider
from infra.workers.celery_app import celery_app

logger = get_logger(__name__)


@celery_app.task(
    name="sms.process_dispatch_job",
    acks_late=True,
    reject_on_worker_lost=True,
    ignore_result=True,
)
def process_dispatch_job(job_id: str) -> None:
    parsed_job_id: UUID | None = None

    try:
        logger.debug(
            "dispatch_task_received",
            extra={"dispatch_job_id": job_id},
        )

        parsed_job_id = UUID(job_id)

        logger.debug(
            "dispatch_task_job_id_parsed",
            extra={"dispatch_job_id": str(parsed_job_id)},
        )

        logger.info(
            "dispatch_job_processing_started",
            extra={
                "dispatch_job_id": str(parsed_job_id),
            },
        )

        with SessionLocal() as session:
            logger.debug(
                "dispatch_task_session_opened",
                extra={"dispatch_job_id": str(parsed_job_id)},
            )

            usecase = DispatchUseCase(
                session=session,
                provider=DummySmsProvider(),
                max_delivery_attempts=5,
            )

            logger.debug(
                "dispatch_task_usecase_started",
                extra={"dispatch_job_id": str(parsed_job_id)},
            )

            usecase.process(parsed_job_id)

            logger.debug(
                "dispatch_task_usecase_finished",
                extra={"dispatch_job_id": str(parsed_job_id)},
            )
    except Exception as exc:
        logger.warning(
            "dispatch_job_processing_failed",
            extra={
                "dispatch_job_id": str(parsed_job_id) if parsed_job_id else job_id,
                "error": repr(exc),
            },
            exc_info=True,
        )
        raise

    logger.info(
        "dispatch_job_processing_finished",
        extra={
            "dispatch_job_id": str(parsed_job_id),
        },
    )

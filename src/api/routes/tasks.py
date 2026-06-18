from typing import Any, cast

from celery.result import AsyncResult
from fastapi import APIRouter, status

from api.schemas.tasks import SampleTaskRequest, TaskQueuedResponse, TaskStatusResponse
from infra.workers.celery_app import celery_app
from infra.workers.sample_tasks import echo_message

router = APIRouter()


def enqueue_echo_message(message: str) -> str:
    task_result: Any = echo_message.delay(message)
    return cast(str, task_result.id)


def read_task_status(task_id: str) -> TaskStatusResponse:
    task_result: Any = AsyncResult(task_id, app=celery_app)
    result: Any | None = task_result.result if task_result.ready() else None
    return TaskStatusResponse(
        task_id=task_id,
        status=cast(str, task_result.status),
        result=result,
    )


@router.post(
    "/sample",
    response_model=TaskQueuedResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def enqueue_sample_task(payload: SampleTaskRequest) -> TaskQueuedResponse:
    task_id = enqueue_echo_message(payload.message)
    return TaskQueuedResponse(task_id=task_id, status="queued")


@router.get("/{task_id}", response_model=TaskStatusResponse)
def get_task_status(task_id: str) -> TaskStatusResponse:
    return read_task_status(task_id)

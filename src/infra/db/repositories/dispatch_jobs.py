from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from domain.enums import MessagePriority
from infra.db.models.dispatch_job import DispatchJob


class DispatchJobRepository:
    def __init__(self, session: Session):
        self.session = session

    def create(self, message_id: UUID, priority: MessagePriority, payload: dict[str, Any]):
        payload["message_id"] = str(message_id)
        payload["cost"] = str(payload["cost"])
        del payload["idempotency_key"]

        job = DispatchJob(message_id=message_id, priority=priority, payload=payload)
        self.session.add(job)
        return job

from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from domain.enums import DispatchJobStatus, MessagePriority
from infra.db.base import Base

if TYPE_CHECKING:
    from infra.db.models.message import Message


class DispatchJob(Base):
    __tablename__ = "dispatch_jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), unique=True, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    priority: Mapped[MessagePriority]
    status: Mapped[DispatchJobStatus] = mapped_column(default=DispatchJobStatus.PENDING)
    retry_count: Mapped[int] = mapped_column(default=0)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    message: Mapped["Message"] = relationship(
        "Message",
        back_populates="dispatch_job",
    )

    __table_args__ = (
        CheckConstraint("retry_count >= 0", name="check_dispatch_job_retry_count_gte_zero"),
        CheckConstraint(
            "updated_at >= created_at", name="check_dispatch_job_updated_at_ge_created_at"
        ),
        Index(
            "ix_dispatch_jobs_ready",
            "status",
            "available_at",
            "priority",
            "created_at",
        ),
    )

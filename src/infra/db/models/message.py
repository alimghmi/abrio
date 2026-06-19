from datetime import datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from domain.enums import MessagePriority, MessageStatus, PaymentStatus  # type: ignore
from infra.db.base import Base

if TYPE_CHECKING:
    from infra.db.models.dispatch_job import DispatchJob
    from infra.db.models.user import User


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    recipient: Mapped[str] = mapped_column(String(13))
    body: Mapped[str] = mapped_column(String(70))
    cost: Mapped[int]
    priority: Mapped[MessagePriority] = mapped_column(index=True)
    idempotency_key: Mapped[UUID] = mapped_column(index=True)
    status: Mapped[MessageStatus] = mapped_column(index=True, default=MessageStatus.QUEUED)
    payment_status: Mapped[PaymentStatus] = mapped_column(default=PaymentStatus.RESERVED)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped["User"] = relationship("User", back_populates="messages")
    dispatch_job: Mapped["DispatchJob | None"] = relationship(
        "DispatchJob",
        back_populates="message",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("updated_at >= created_at", name="check_updated_at_ge_created_at"),
        CheckConstraint("cost > 0", name="check_cost_gt_zero"),
        UniqueConstraint("user_id", "idempotency_key", name="unique_user_id_idempotency_key"),
    )

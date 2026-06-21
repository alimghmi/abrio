from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Numeric, func
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db.base import Base

if TYPE_CHECKING:
    from infra.db.models.user import User


class Balance(Base):
    __tablename__ = "balances"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    credits: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    reserved_credits: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0.00"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    user: Mapped["User"] = relationship("User", back_populates="balance")

    __table_args__ = (
        CheckConstraint("credits >= 0", name="check_credits_non_negative"),
        CheckConstraint("credits >= reserved_credits", name="check_credits_ge_reserved_credits"),
        CheckConstraint("reserved_credits >= 0", name="check_reserved_credits_non_negative"),
        CheckConstraint("updated_at >= created_at", name="check_updated_at_ge_created_at"),
    )

    @hybrid_property
    def available_credits(self) -> Decimal:
        return self.credits - self.reserved_credits

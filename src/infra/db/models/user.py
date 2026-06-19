from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import CheckConstraint, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from infra.db.base import Base

if TYPE_CHECKING:
    from infra.db.models.balance import Balance
    from infra.db.models.message import Message


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint("updated_at >= created_at", name="check_updated_at_ge_created_at"),
    )

    balance: Mapped["Balance"] = relationship(
        "Balance",
        back_populates="user",
        uselist=False,
    )

    messages: Mapped[list["Message"]] = relationship(
        "Message",
        back_populates="user",
    )

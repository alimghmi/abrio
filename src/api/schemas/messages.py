from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer

from core.config import get_settings
from domain.enums import MessagePriority, MessageStatus, PaymentStatus

settings = get_settings()


class MessageRequest(BaseModel):
    user_id: int
    # Matches +989121234567, 9121234567, and 09121234567:
    recipient: str = Field(pattern=r"^(\+98|0)?9\d{9}$")
    # Single page SMS max length:
    body: str = Field(min_length=1, max_length=70)
    priority: MessagePriority
    idempotency_key: UUID


class BatchMessageRequestItem(BaseModel):
    recipient: str = Field(pattern=r"^(\+98|0)?9\d{9}$")
    body: str = Field(min_length=1, max_length=70)
    priority: MessagePriority
    idempotency_key: UUID


class BatchMessageRequest(BaseModel):
    user_id: int
    messages: list[BatchMessageRequestItem] = Field(
        min_length=1, max_length=settings.max_messages_per_batch
    )


class MessageResponse(BaseModel):
    id: UUID
    user_id: int
    recipient: str
    body: str
    cost: Decimal = Field(examples=["1.00"])
    priority: MessagePriority
    idempotency_key: UUID
    status: MessageStatus
    payment_status: PaymentStatus
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("cost")
    def serialize_decimal(self, value: Decimal) -> str:
        return f"{value:.2f}"


class BatchMessageResponse(BaseModel):
    created_count: int
    messages: list[MessageResponse]


class MessagesSummaryResponse(BaseModel):
    user_id: int
    total: int
    queued: int
    dispatching: int
    failed: int
    sent: int
    permanent_failed: int

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from domain.enums import MessagePriority, MessageStatus, PaymentStatus


class MessageRequest(BaseModel):
    user_id: int
    # Matches +989121234567, 9121234567, and 09121234567:
    recipient: str = Field(pattern=r"^(\+98|0)?9\d{9}$")
    # Single page SMS max length:
    body: str = Field(min_length=1, max_length=70)
    priority: MessagePriority
    idempotency_key: UUID


class MessageResponse(BaseModel):
    id: UUID
    user_id: int
    recipient: str
    body: str
    priority: MessagePriority
    idempotency_key: UUID
    status: MessageStatus
    payment_status: PaymentStatus
    created_at: datetime
    updated_at: datetime

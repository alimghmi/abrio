from enum import StrEnum


class MessagePriority(StrEnum):
    NORMAL = "normal"
    EXPRESS = "express"


class MessageStatus(StrEnum):
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    FAILED = "failed"
    SENT = "sent"
    PERMANENT_FAILED = "permanent_failed"


class PaymentStatus(StrEnum):
    RESERVED = "reserved"
    DEDUCTED = "deducted"
    REFUNDED = "refunded"


class DispatchJobStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    RETRY = "retry"
    COMPLETED = "completed"
    FAILED = "failed"

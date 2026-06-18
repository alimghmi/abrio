from enum import Enum


class MessagePriority(Enum):
    NORMAL = "normal"
    EXPRESS = "express"


class MessageStatus(Enum):
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    FAILED = "failed"
    SENT = "sent"
    PERMANENT_FAILED = "permanent_failed"


class PaymentStatus(Enum):
    RESERVED = "reserved"
    DEDUCTED = "deducted"
    REFUNDED = "refunded"

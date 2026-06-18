from enum import Enum


class MessagePriority(Enum):
    NORMAL = "normal"
    EXPRESS = "express"


class MessageStatus(Enum):
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    SENT = "sent"
    FAILED = "failed"
    DELIVERED = "delivered"


class PaymentStatus(Enum):
    RESERVED = "reserved"
    DEDUCTED = "deducted"
    REFUNDED = "refunded"

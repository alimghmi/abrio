from decimal import Decimal
from uuid import UUID

from fastapi import status


class AppError(Exception):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "app_error"

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class UserNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "user_not_found"

    def __init__(self, user_id: int):
        super().__init__(f"User with id={user_id} was not found.")
        self.user_id = user_id


class InvalidAmountValueError(AppError):
    code = "invalid_amount_value"

    def __init__(self, amount: Decimal):
        super().__init__(f"Amount={amount} must be greater than zero.")
        self.amount = amount


class MessageNotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "message_not_found"

    def __init__(self, message_id: UUID | None = None, idempotency_key: UUID | None = None):
        if message_id:
            prefix, id = "message_", message_id
        elif idempotency_key:
            prefix, id = "idempotency", idempotency_key
        else:
            prefix, id = "", "undefined"

        super().__init__(f"Message with {prefix}id={id} was not found.")
        self.message_id = message_id
        self.idempotency_key = idempotency_key


class IdempotencyConflictError(AppError):
    code = "idempotency_conflict"

    def __init__(self, idempotency_id: UUID | None = None, extra: str | None = None):
        extra_message = ""
        if idempotency_id is not None:
            extra_message += f"={idempotency_id}"
        if extra is not None:
            extra_message += f" {extra}"

        super().__init__(f"Idempotency key{extra_message} conflict.")
        self.idempotency_id = idempotency_id
        self.extra = extra


class IdempotencyDuplicateError(AppError):
    code = "idempotency_duplicate"

    def __init__(self, idempotency_id: UUID):
        super().__init__(f"Batch request idempotency key={idempotency_id} duplicate.")
        self.idempotency_id = idempotency_id


class InsufficientBalanceError(AppError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "insufficient_balance"

    def __init__(self, user_id: int, message_cost: Decimal):
        super().__init__(
            f"User={user_id} balance insufficient (need {message_cost}) to send message."
        )
        self.user_id = user_id
        self.message_cost = message_cost

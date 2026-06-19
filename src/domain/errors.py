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

    def __init__(self, idempotency_id: UUID, extra: str = ""):
        if extra:
            extra = f" {extra}"
        super().__init__(f"Idempotency key={idempotency_id}{extra} conflict.")
        self.idempotency_id = idempotency_id
        self.extra = extra


class InsufficientBalanceError(AppError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "insufficient_balance"

    def __init__(self, user_id: int, message_cost: int):
        super().__init__(
            f"User={user_id} balance insufficient (need {message_cost}) to send message."
        )
        self.user_id = user_id
        self.message_cost = message_cost

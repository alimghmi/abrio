from decimal import Decimal
from uuid import UUID

from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.schemas.messages import (
    BatchMessageRequest,
    BatchMessageRequestItem,
    BatchMessageResponse,
    MessageRequest,
    MessageResponse,
)
from api.schemas.pagination import PaginatedResponse, PaginationParams  # type: ignore
from core.config import Settings
from domain.enums import MessagePriority
from domain.errors import (
    IdempotencyConflictError,
    IdempotencyDuplicateError,
    InsufficientBalanceError,
    UserNotFoundError,
)
from infra.db.models.message import Message
from infra.db.repositories.balance import BalanceRepositry
from infra.db.repositories.dispatch_jobs import DispatchJobRepository
from infra.db.repositories.messages import MessageRepository


class MessageUseCase:
    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings
        self._repo = MessageRepository(session)
        self._balance_repo = BalanceRepositry(session)
        self._dispatch_repo = DispatchJobRepository(session)

    def get_user_messages(self, user_id: int) -> list[Message]:
        return self._repo.get_user_messages(user_id=user_id)

    def get_messages_slice(self, params: PaginationParams, **filters) -> PaginatedResponse:
        messages, total = self._repo.get_messages_slice(
            limit=params.limit, offset=params.offset, **filters
        )
        return PaginatedResponse.make(items=messages, total=total, params=params)

    def get_user_message(self, message_id: UUID, user_id: int) -> Message:
        return self._repo.get_user_message(message_id=message_id, user_id=user_id)

    def create_message(self, payload: MessageRequest) -> Message:
        message_cost = self._get_message_cost(payload)
        payload_dict = {**payload.model_dump(), "cost": message_cost}
        try:
            with self.session.begin():
                message = self._repo.create_message(payload=payload_dict)
                self.session.flush()
                self._dispatch_repo.create(
                    message_id=message.id, priority=payload.priority, payload=payload_dict
                )
                self._balance_repo.reserve_credits(payload.user_id, message_cost)
        except IntegrityError as e:
            constraint_name = getattr(
                getattr(e.orig, "diag", None),
                "constraint_name",
                None,
            )
            if isinstance(e.orig, UniqueViolation):
                return self._repo.get_user_message_by_idempotency_key(
                    payload.user_id, payload.idempotency_key
                )

            elif isinstance(e.orig, ForeignKeyViolation):
                raise UserNotFoundError(user_id=payload.user_id) from e

            elif (
                isinstance(e.orig, CheckViolation)
                and constraint_name == "check_credits_ge_reserved_credits"
            ):
                raise InsufficientBalanceError(payload.user_id, message_cost=message_cost) from e

            raise e

        return message

    def batch_create_message(self, payload: BatchMessageRequest) -> BatchMessageResponse:
        message_pairs: list[tuple[Message, BatchMessageRequestItem]] = []
        duplicate_idempotency_key = self._check_duplicate_idempotency_keys(payload)
        if duplicate_idempotency_key is not None:
            raise IdempotencyDuplicateError(duplicate_idempotency_key)

        user_id = payload.user_id
        total_batch_cost = self._calculate_batch_message_cost(payload.messages)

        try:
            with self.session.begin():
                for item in payload.messages:
                    message_cost = self._get_message_cost(item)
                    message_dict = {
                        **item.model_dump(),
                        "cost": message_cost,
                        "user_id": user_id,
                    }
                    message_instance = self._repo.create_message(message_dict)
                    message_pairs.append((message_instance, item))

                self.session.flush()

                for message_instance, item in message_pairs:
                    message_cost = self._get_message_cost(item)
                    message_dict = {
                        **item.model_dump(),
                        "cost": message_cost,
                        "user_id": user_id,
                    }
                    self._dispatch_repo.create(
                        message_id=message_instance.id,
                        priority=item.priority,
                        payload=message_dict,
                    )

                self.session.flush()

                self._balance_repo.reserve_credits(payload.user_id, total_batch_cost)

        except IntegrityError as e:
            constraint_name = getattr(
                getattr(e.orig, "diag", None),
                "constraint_name",
                None,
            )
            if isinstance(e.orig, UniqueViolation):
                raise IdempotencyConflictError from e

            elif isinstance(e.orig, ForeignKeyViolation):
                raise UserNotFoundError(user_id=payload.user_id) from e

            elif (
                isinstance(e.orig, CheckViolation)
                and constraint_name == "check_credits_ge_reserved_credits"
            ):
                raise InsufficientBalanceError(
                    payload.user_id, message_cost=total_batch_cost
                ) from e

            raise e

        message_instances = [
            MessageResponse.model_validate(message_instance)
            for message_instance, _ in message_pairs
        ]
        return BatchMessageResponse(
            created_count=len(message_instances), messages=message_instances
        )

    def calculate_summary(self, user_id: int) -> dict[str, int | dict]:
        return self._repo.calculate_summary(user_id=user_id)

    def _get_message_cost(self, message: MessageRequest | BatchMessageRequestItem) -> Decimal:
        if message.priority == MessagePriority.NORMAL:
            return self.settings.cost_per_message
        elif message.priority == MessagePriority.EXPRESS:
            return self.settings.cost_per_express_message
        else:
            raise ValueError(f"Invalid message priority={message.priority}")

    def _calculate_batch_message_cost(self, messages: list[BatchMessageRequestItem]) -> Decimal:
        cost = Decimal("0.00")
        for message in messages:
            cost += self._get_message_cost(message)
        return cost

    @staticmethod
    def _check_duplicate_idempotency_keys(payload: BatchMessageRequest) -> UUID | None:
        seen = set()
        for message in payload.messages:
            if message.idempotency_key not in seen:
                seen.add(message.idempotency_key)
            else:
                return message.idempotency_key

        return None

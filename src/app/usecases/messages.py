from collections import Counter
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
from core.logging import get_logger
from core.metrics import (
    record_idempotent_replay,
    record_messages_submitted,
    record_submission_rejected,
)
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

logger = get_logger(__name__)


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
                message = self._repo.get_user_message_by_idempotency_key(
                    payload.user_id, payload.idempotency_key
                )
                record_idempotent_replay(submission_type="single")
                logger.info(
                    "message_idempotent_replay",
                    extra={
                        "message_id": str(message.id),
                        "priority": message.priority.value,
                        "submission_type": "single",
                    },
                )
                return message

            elif isinstance(e.orig, ForeignKeyViolation):
                _record_submission_rejection(reason="invalid_request", submission_type="single")
                raise UserNotFoundError(user_id=payload.user_id) from e

            elif (
                isinstance(e.orig, CheckViolation)
                and constraint_name == "check_credits_ge_reserved_credits"
            ):
                _record_submission_rejection(
                    reason="insufficient_balance", submission_type="single"
                )
                raise InsufficientBalanceError(payload.user_id, message_cost=message_cost) from e

            _record_submission_rejection(reason="database_error", submission_type="single")
            raise e

        record_messages_submitted(
            priority=payload.priority,
            submission_type="single",
            count=1,
        )
        logger.info(
            "message_submission_accepted",
            extra={
                "message_id": str(message.id),
                "priority": payload.priority.value,
                "submission_type": "single",
                "message_count": 1,
            },
        )
        return message

    def batch_create_message(self, payload: BatchMessageRequest) -> BatchMessageResponse:
        message_pairs: list[tuple[Message, BatchMessageRequestItem]] = []
        duplicate_idempotency_key = self._check_duplicate_idempotency_keys(payload)
        if duplicate_idempotency_key is not None:
            _record_submission_rejection(reason="idempotency_conflict", submission_type="batch")
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
                _record_submission_rejection(reason="idempotency_conflict", submission_type="batch")
                raise IdempotencyConflictError from e

            elif isinstance(e.orig, ForeignKeyViolation):
                _record_submission_rejection(reason="invalid_request", submission_type="batch")
                raise UserNotFoundError(user_id=payload.user_id) from e

            elif (
                isinstance(e.orig, CheckViolation)
                and constraint_name == "check_credits_ge_reserved_credits"
            ):
                _record_submission_rejection(reason="insufficient_balance", submission_type="batch")
                raise InsufficientBalanceError(
                    payload.user_id, message_cost=total_batch_cost
                ) from e

            _record_submission_rejection(reason="database_error", submission_type="batch")
            raise e

        message_instances = [
            MessageResponse.model_validate(message_instance)
            for message_instance, _ in message_pairs
        ]
        priority_counts = Counter(item.priority for _, item in message_pairs)
        for priority, count in priority_counts.items():
            record_messages_submitted(
                priority=priority,
                submission_type="batch",
                count=count,
            )
        logger.info(
            "message_submission_accepted",
            extra={
                "submission_type": "batch",
                "message_count": len(message_instances),
                "priority_counts": {
                    priority.value: count for priority, count in priority_counts.items()
                },
            },
        )
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


def _record_submission_rejection(*, reason: str, submission_type: str) -> None:
    record_submission_rejected(reason=reason, submission_type=submission_type)
    logger.info(
        "message_submission_rejected",
        extra={
            "reason": reason,
            "submission_type": submission_type,
        },
    )

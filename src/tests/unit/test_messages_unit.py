from datetime import UTC, datetime
from decimal import Decimal
from types import TracebackType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.routes import messages as message_routes
from api.schemas.messages import (
    BatchMessageRequest,
    BatchMessageResponse,
    MessageRequest,
    MessageResponse,
)
from api.schemas.messages import (
    settings as message_schema_settings,
)
from api.schemas.pagination import PaginatedResponse, PaginationParams
from app.usecases.messages import MessageUseCase
from core.config import Settings
from domain.enums import MessagePriority, MessageStatus, PaymentStatus
from domain.errors import (
    IdempotencyConflictError,
    IdempotencyDuplicateError,
    InsufficientBalanceError,
    UserNotFoundError,
)
from main import create_app
from tests.conftest import MessageRequestFactory

pytestmark = pytest.mark.unit


class FakeMessage:
    def __init__(
        self,
        *,
        message_id: UUID | None = None,
        user_id: int = 1,
        recipient: str = "+989121234567",
        body: str = "hello",
        cost: Decimal = Decimal("1.00"),
        priority: MessagePriority = MessagePriority.NORMAL,
        idempotency_key: UUID | None = None,
        status: MessageStatus = MessageStatus.QUEUED,
        payment_status: PaymentStatus = PaymentStatus.RESERVED,
    ) -> None:
        self.id = message_id or uuid4()
        self.user_id = user_id
        self.recipient = recipient
        self.body = body
        self.cost = cost
        self.priority = priority
        self.idempotency_key = idempotency_key or uuid4()
        self.status = status
        self.payment_status = payment_status
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)


class FakeMessageUseCase:
    def __init__(self) -> None:
        self.message = FakeMessage()

    def create_message(self, payload: MessageRequest) -> FakeMessage:
        return FakeMessage(
            user_id=payload.user_id,
            recipient=payload.recipient,
            body=payload.body,
            priority=payload.priority,
            idempotency_key=payload.idempotency_key,
        )

    def batch_create_message(self, payload: BatchMessageRequest) -> BatchMessageResponse:
        messages = [
            FakeMessage(
                user_id=payload.user_id,
                recipient=message.recipient,
                body=message.body,
                priority=message.priority,
                idempotency_key=message.idempotency_key,
            )
            for message in payload.messages
        ]
        return BatchMessageResponse(
            created_count=len(messages),
            messages=[MessageResponse.model_validate(message) for message in messages],
        )

    def get_messages_slice(
        self,
        *,
        params: PaginationParams,
        **filters: object,
    ) -> PaginatedResponse[Any]:
        assert params.page == 2
        assert filters["user_id"] == 1
        return PaginatedResponse[Any].make(items=[self.message], total=1, params=params)

    def calculate_summary(self, user_id: int) -> dict[str, int]:
        assert user_id == 1
        return {
            "user_id": user_id,
            "total": 1,
            "queued": 1,
            "dispatching": 0,
            "failed": 0,
            "sent": 0,
            "permanent_failed": 0,
        }

    def get_user_message(self, message_id: UUID, user_id: int) -> FakeMessage:
        assert user_id == 1
        self.message.id = message_id
        return self.message


class FakeTransaction:
    def __enter__(self) -> "FakeTransaction":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class FakeSession:
    def __init__(self) -> None:
        self.begin_count = 0
        self.flush_called = False

    def begin(self) -> FakeTransaction:
        self.begin_count += 1
        return FakeTransaction()

    def flush(self) -> None:
        self.flush_called = True


class IntegrityFailingMessageRepository:
    def __init__(self, error: IntegrityError, existing_message: FakeMessage | None = None) -> None:
        self.error = error
        self.existing_message = existing_message or FakeMessage()
        self.lookup_count = 0
        self.create_count = 0

    def create_message(self, payload: dict[str, object]) -> FakeMessage:
        _ = payload
        self.create_count += 1
        raise self.error

    def get_user_message_by_idempotency_key(
        self, user_id: int, idempotency_key: UUID
    ) -> FakeMessage:
        self.lookup_count += 1
        self.existing_message.user_id = user_id
        self.existing_message.idempotency_key = idempotency_key
        return self.existing_message


class FakeMessageRepository:
    def __init__(self) -> None:
        self.messages: list[FakeMessage] = []

    def create_message(self, payload: dict[str, object]) -> FakeMessage:
        message = FakeMessage(
            user_id=cast(int, payload["user_id"]),
            recipient=cast(str, payload["recipient"]),
            body=cast(str, payload["body"]),
            cost=cast(Decimal, payload["cost"]),
            priority=cast(MessagePriority, payload["priority"]),
            idempotency_key=cast(UUID, payload["idempotency_key"]),
        )
        self.messages.append(message)
        return message


class CountingBalanceRepository:
    def __init__(self) -> None:
        self.reserve_count = 0

    def reserve_credits(self, user_id: int, amount: Decimal) -> None:
        _ = (user_id, amount)
        self.reserve_count += 1


class CountingDispatchRepository:
    def __init__(self) -> None:
        self.create_count = 0

    def create(
        self,
        *,
        message_id: UUID,
        priority: MessagePriority,
        payload: dict[str, Any],
    ) -> None:
        _ = (message_id, priority, payload)
        self.create_count += 1


class BalanceIntegrityFailingRepository:
    def __init__(self, error: IntegrityError) -> None:
        self.error = error

    def reserve_credits(self, user_id: int, amount: Decimal) -> None:
        _ = (user_id, amount)
        raise self.error


class FakeConstraintDiag:
    constraint_name = "check_credits_ge_reserved_credits"


class FakeBalanceCheckViolation(CheckViolation):
    @property
    def diag(self) -> FakeConstraintDiag:
        return FakeConstraintDiag()


def test_send_message_route_uses_message_usecase(
    message_request_factory: MessageRequestFactory,
) -> None:
    payload = message_request_factory(body="hello route")

    response = message_routes.send_message(payload, cast(MessageUseCase, FakeMessageUseCase()))

    assert response.body == "hello route"
    assert response.user_id == payload.user_id
    assert response.idempotency_key == payload.idempotency_key


def test_batch_send_message_route_uses_message_usecase() -> None:
    idempotency_key = uuid4()
    payload = BatchMessageRequest(
        user_id=1,
        messages=[
            {
                "recipient": "+989121234567",
                "body": "hello batch",
                "priority": MessagePriority.NORMAL,
                "idempotency_key": idempotency_key,
            }
        ],
    )

    response = message_routes.batch_send_message(
        payload,
        cast(MessageUseCase, FakeMessageUseCase()),
    )

    assert response.created_count == 1
    assert response.messages[0].body == "hello batch"
    assert response.messages[0].idempotency_key == idempotency_key


def test_get_messages_route_uses_message_usecase() -> None:
    response = message_routes.get_messages(
        user_id=1,
        params=PaginationParams(page=2, size=10),
        usecase=cast(MessageUseCase, FakeMessageUseCase()),
    )

    assert response.total == 1
    assert response.page == 2
    assert response.items[0].user_id == 1


def test_get_messages_summary_route_uses_message_usecase() -> None:
    response = message_routes.get_user_messages_summary(
        1,
        cast(MessageUseCase, FakeMessageUseCase()),
    )

    assert response == {
        "user_id": 1,
        "total": 1,
        "queued": 1,
        "dispatching": 0,
        "failed": 0,
        "sent": 0,
        "permanent_failed": 0,
    }


def test_get_message_by_id_route_uses_message_usecase() -> None:
    message_id = uuid4()

    response = message_routes.get_message_by_id(
        message_id,
        user_id=1,
        usecase=cast(MessageUseCase, FakeMessageUseCase()),
    )

    assert response.id == message_id
    assert response.user_id == 1


@pytest.mark.parametrize("recipient", ["+989121234567", "09121234567", "9121234567"])
def test_message_request_accepts_valid_iranian_mobile_numbers(recipient: str) -> None:
    payload = MessageRequest(
        user_id=1,
        recipient=recipient,
        body="hello",
        priority=MessagePriority.NORMAL,
        idempotency_key=uuid4(),
    )

    assert payload.recipient == recipient


@pytest.mark.parametrize("recipient", ["", "12345", "+971912123456", "0912123456a"])
def test_message_request_rejects_invalid_recipients(recipient: str) -> None:
    with pytest.raises(ValidationError):
        MessageRequest(
            user_id=1,
            recipient=recipient,
            body="hello",
            priority=MessagePriority.NORMAL,
            idempotency_key=uuid4(),
        )


def test_message_request_rejects_invalid_body_lengths() -> None:
    with pytest.raises(ValidationError):
        MessageRequest(
            user_id=1,
            recipient="+989121234567",
            body="",
            priority=MessagePriority.NORMAL,
            idempotency_key=uuid4(),
        )

    with pytest.raises(ValidationError):
        MessageRequest(
            user_id=1,
            recipient="+989121234567",
            body="x" * 71,
            priority=MessagePriority.NORMAL,
            idempotency_key=uuid4(),
        )


def test_message_request_rejects_invalid_priority() -> None:
    with pytest.raises(ValidationError):
        MessageRequest(
            user_id=1,
            recipient="+989121234567",
            body="hello",
            priority=cast(MessagePriority, "urgent"),
            idempotency_key=uuid4(),
        )


def batch_message_payloads(count: int, idempotency_key: UUID | None = None) -> list[dict[str, Any]]:
    return [
        {
            "recipient": "+989121234567",
            "body": f"hello {index}",
            "priority": MessagePriority.NORMAL,
            "idempotency_key": idempotency_key or uuid4(),
        }
        for index in range(count)
    ]


def test_batch_message_request_accepts_one_and_max_messages() -> None:
    one_message_payload = BatchMessageRequest(user_id=1, messages=batch_message_payloads(1))
    max_message_payload = BatchMessageRequest(
        user_id=1,
        messages=batch_message_payloads(message_schema_settings.max_messages_per_batch),
    )

    assert len(one_message_payload.messages) == 1
    assert len(max_message_payload.messages) == message_schema_settings.max_messages_per_batch


def test_batch_message_request_rejects_empty_or_too_large_batches() -> None:
    with pytest.raises(ValidationError):
        BatchMessageRequest(user_id=1, messages=[])

    with pytest.raises(ValidationError):
        BatchMessageRequest(
            user_id=1,
            messages=batch_message_payloads(message_schema_settings.max_messages_per_batch + 1),
        )


def test_batch_create_message_rejects_duplicate_idempotency_keys_before_db_work() -> None:
    idempotency_key = uuid4()
    payload = BatchMessageRequest(
        user_id=1,
        messages=batch_message_payloads(2, idempotency_key=idempotency_key),
    )
    session = FakeSession()
    usecase = MessageUseCase(cast(Session, session), Settings(cost_per_message=Decimal("1.00")))

    with pytest.raises(IdempotencyDuplicateError) as exc_info:
        usecase.batch_create_message(payload)

    assert exc_info.value.idempotency_id == idempotency_key
    assert session.begin_count == 0
    assert session.flush_called is False


def test_create_message_returns_existing_message_for_postgres_unique_violation(
    message_request_factory: MessageRequestFactory,
) -> None:
    payload = message_request_factory(user_id=7)
    repo = IntegrityFailingMessageRepository(
        IntegrityError("stmt", {}, UniqueViolation("duplicate idempotency key"))
    )
    balance_repo = CountingBalanceRepository()
    dispatch_repo = CountingDispatchRepository()
    session = FakeSession()
    usecase = MessageUseCase(cast(Session, session), Settings(cost_per_message=Decimal("3.00")))
    usecase._repo = cast(Any, repo)
    usecase._balance_repo = cast(Any, balance_repo)
    usecase._dispatch_repo = cast(Any, dispatch_repo)

    response = usecase.create_message(payload)

    assert response.idempotency_key == payload.idempotency_key
    assert response.user_id == payload.user_id
    assert repo.lookup_count == 1
    assert balance_repo.reserve_count == 1
    assert dispatch_repo.create_count == 0
    assert session.flush_called is False


def test_create_message_maps_postgres_foreign_key_violation_to_user_not_found(
    message_request_factory: MessageRequestFactory,
) -> None:
    payload = message_request_factory(user_id=77)
    repo = IntegrityFailingMessageRepository(
        IntegrityError("stmt", {}, ForeignKeyViolation("missing user"))
    )
    usecase = MessageUseCase(
        cast(Session, FakeSession()), Settings(cost_per_message=Decimal("3.00"))
    )
    usecase._repo = cast(Any, repo)
    usecase._balance_repo = cast(Any, CountingBalanceRepository())
    usecase._dispatch_repo = cast(Any, CountingDispatchRepository())

    with pytest.raises(UserNotFoundError) as exc_info:
        usecase.create_message(payload)

    assert exc_info.value.user_id == 77


def test_create_message_maps_postgres_balance_check_violation_to_insufficient_balance(
    message_request_factory: MessageRequestFactory,
) -> None:
    payload = message_request_factory(user_id=88)
    usecase = MessageUseCase(
        cast(Session, FakeSession()), Settings(cost_per_message=Decimal("5.00"))
    )
    usecase._repo = cast(
        Any, IntegrityFailingMessageRepository(IntegrityError("stmt", {}, Exception("unused")))
    )
    usecase._balance_repo = cast(
        Any,
        BalanceIntegrityFailingRepository(
            IntegrityError("stmt", {}, FakeBalanceCheckViolation("balance check failed"))
        ),
    )
    usecase._dispatch_repo = cast(Any, CountingDispatchRepository())

    with pytest.raises(InsufficientBalanceError) as exc_info:
        usecase.create_message(payload)

    assert exc_info.value.user_id == 88
    assert exc_info.value.message_cost == Decimal("5.00")


def test_batch_create_message_maps_postgres_unique_violation_to_idempotency_conflict() -> None:
    payload = BatchMessageRequest(user_id=1, messages=batch_message_payloads(1))
    usecase = MessageUseCase(
        cast(Session, FakeSession()), Settings(cost_per_message=Decimal("1.00"))
    )
    usecase._repo = cast(
        Any,
        IntegrityFailingMessageRepository(
            IntegrityError("stmt", {}, UniqueViolation("duplicate idempotency key"))
        ),
    )
    usecase._balance_repo = cast(Any, CountingBalanceRepository())
    usecase._dispatch_repo = cast(Any, CountingDispatchRepository())

    with pytest.raises(IdempotencyConflictError):
        usecase.batch_create_message(payload)


def test_batch_create_message_maps_postgres_foreign_key_violation_to_user_not_found() -> None:
    payload = BatchMessageRequest(user_id=404, messages=batch_message_payloads(1))
    usecase = MessageUseCase(
        cast(Session, FakeSession()), Settings(cost_per_message=Decimal("1.00"))
    )
    usecase._repo = cast(
        Any,
        IntegrityFailingMessageRepository(
            IntegrityError("stmt", {}, ForeignKeyViolation("missing user"))
        ),
    )
    usecase._balance_repo = cast(Any, CountingBalanceRepository())
    usecase._dispatch_repo = cast(Any, CountingDispatchRepository())

    with pytest.raises(UserNotFoundError) as exc_info:
        usecase.batch_create_message(payload)

    assert exc_info.value.user_id == 404


def test_batch_create_message_maps_postgres_balance_check_violation_to_insufficient_balance() -> (
    None
):
    payload = BatchMessageRequest(user_id=1, messages=batch_message_payloads(2))
    usecase = MessageUseCase(
        cast(Session, FakeSession()), Settings(cost_per_message=Decimal("2.00"))
    )
    usecase._repo = cast(Any, FakeMessageRepository())
    usecase._balance_repo = cast(
        Any,
        BalanceIntegrityFailingRepository(
            IntegrityError("stmt", {}, FakeBalanceCheckViolation("balance check failed"))
        ),
    )
    usecase._dispatch_repo = cast(Any, CountingDispatchRepository())

    with pytest.raises(InsufficientBalanceError) as exc_info:
        usecase.batch_create_message(payload)

    assert exc_info.value.user_id == 1
    assert exc_info.value.message_cost == Decimal("4.00")


def test_create_message_reraises_unknown_integrity_error(
    message_request_factory: MessageRequestFactory,
) -> None:
    payload = message_request_factory()
    repo = IntegrityFailingMessageRepository(IntegrityError("stmt", {}, Exception("unknown")))
    usecase = MessageUseCase(
        cast(Session, FakeSession()), Settings(cost_per_message=Decimal("3.00"))
    )
    usecase._repo = cast(Any, repo)
    usecase._balance_repo = cast(Any, CountingBalanceRepository())
    usecase._dispatch_repo = cast(Any, CountingDispatchRepository())

    with pytest.raises(IntegrityError):
        usecase.create_message(payload)


def test_message_routes_are_registered_in_app() -> None:
    app = create_app()
    route_paths = set(app.openapi()["paths"])

    assert "/api/v1/messages/" in route_paths
    assert "/api/v1/messages/batch" in route_paths
    assert "/api/v1/messages/summary" in route_paths
    assert "/api/v1/messages/{message_id}" in route_paths

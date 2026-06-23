from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace, TracebackType
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from prometheus_client import generate_latest
from psycopg.errors import UniqueViolation
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.schemas.messages import BatchMessageRequest, MessageRequest
from app.usecases.messages import MessageUseCase
from core.config import Settings
from core.metrics import (
    REGISTRY,
    collect_database_metrics,
    record_http_request,
    record_relay_publish,
    record_relay_publish_failure,
    reset_metrics_for_tests,
)
from core.observability import normalized_route
from core.request_context import get_request_id
from domain.enums import DispatchJobStatus, MessagePriority, MessageStatus, PaymentStatus
from infra.db.models.dispatch_job import DispatchJob
from infra.db.models.message import Message
from main import create_app
from tests.conftest import MessageRequestFactory, SeedUser, SessionFactory

pytestmark = pytest.mark.unit


class FakeTransaction:
    def __enter__(self) -> FakeTransaction:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None


class FakeSession:
    def begin(self) -> FakeTransaction:
        return FakeTransaction()

    def flush(self) -> None:
        return None


class FakeMessage:
    def __init__(
        self,
        *,
        user_id: int = 1,
        recipient: str = "+989121234567",
        body: str = "hello",
        cost: Decimal = Decimal("1.00"),
        priority: MessagePriority = MessagePriority.NORMAL,
        idempotency_key: UUID | None = None,
    ) -> None:
        self.id = uuid4()
        self.user_id = user_id
        self.recipient = recipient
        self.body = body
        self.cost = cost
        self.priority = priority
        self.idempotency_key = idempotency_key or uuid4()
        self.status = MessageStatus.QUEUED
        self.payment_status = PaymentStatus.RESERVED
        self.created_at = datetime.now(UTC)
        self.updated_at = datetime.now(UTC)


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

    def get_user_message_by_idempotency_key(
        self, user_id: int, idempotency_key: UUID
    ) -> FakeMessage:
        return FakeMessage(user_id=user_id, idempotency_key=idempotency_key)


class UniqueViolationRepository(FakeMessageRepository):
    def create_message(self, payload: dict[str, object]) -> FakeMessage:
        _ = payload
        raise IntegrityError("stmt", {}, UniqueViolation("duplicate idempotency key"))


class FakeBalanceRepository:
    def reserve_credits(self, user_id: int, amount: Decimal) -> None:
        _ = (user_id, amount)


class FakeDispatchRepository:
    def create(
        self,
        *,
        message_id: UUID,
        priority: MessagePriority,
        payload: dict[str, Any],
    ) -> None:
        _ = (message_id, priority, payload)


@pytest.fixture(autouse=True)
def reset_metrics() -> None:
    reset_metrics_for_tests()


def metrics_text() -> str:
    return generate_latest(REGISTRY).decode("utf-8")


def run_asgi_get(
    app,
    path: str,
    *,
    query_string: bytes = b"",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> tuple[int, dict[str, str], bytes]:
    messages: list[dict[str, Any]] = []
    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if request_sent:
            await asyncio.sleep(0)
            return {"type": "http.disconnect"}
        request_sent = True
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "method": "GET",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "headers": headers or [],
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
        "scheme": "http",
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))

    response_start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    response_headers = {
        name.decode("latin-1"): value.decode("latin-1")
        for name, value in response_start.get("headers", [])
    }
    return int(response_start["status"]), response_headers, body


def build_message_usecase(repository: object) -> MessageUseCase:
    usecase = MessageUseCase(
        cast(Session, FakeSession()),
        Settings(cost_per_message=Decimal("1.00"), cost_per_express_message=Decimal("3.00")),
    )
    usecase._repo = cast(Any, repository)
    usecase._balance_repo = cast(Any, FakeBalanceRepository())
    usecase._dispatch_repo = cast(Any, FakeDispatchRepository())
    return usecase


def test_metrics_endpoint_is_available(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("core.metrics.collect_database_metrics", lambda: None)
    app = create_app()

    status_code, headers, _body = run_asgi_get(app, "/metrics")

    assert status_code == 200
    assert "text/plain" in headers["content-type"]


def test_http_metrics_use_normalized_route_labels() -> None:
    message_id = uuid4()
    scope = {"route": SimpleNamespace(path="/api/v1/messages/{message_id}")}

    route = normalized_route(cast(Any, scope))
    record_http_request(
        method="GET",
        route=route,
        status_code=200,
        duration_seconds=0.01,
    )

    text = metrics_text()
    assert f'route="/api/v1/messages/{message_id}"' not in text
    assert 'route="/api/v1/messages/{message_id}"' in text


def test_request_id_is_returned_and_context_is_reset() -> None:
    app = create_app()
    request_id = str(uuid4())

    status_code, headers, _body = run_asgi_get(
        app,
        "/api/v1/health/live",
        headers=[(b"x-request-id", request_id.encode())],
    )

    assert status_code == 200
    assert headers["x-request-id"] == request_id
    assert get_request_id() is None


def test_single_and_batch_submission_counters(
    message_request_factory: MessageRequestFactory,
) -> None:
    usecase = build_message_usecase(FakeMessageRepository())
    single = message_request_factory(priority=MessagePriority.NORMAL)
    batch = BatchMessageRequest(
        user_id=1,
        messages=[
            {
                "recipient": "+989121234567",
                "body": "normal",
                "priority": MessagePriority.NORMAL,
                "idempotency_key": uuid4(),
            },
            {
                "recipient": "+989121234567",
                "body": "express",
                "priority": MessagePriority.EXPRESS,
                "idempotency_key": uuid4(),
            },
            {
                "recipient": "+989121234567",
                "body": "normal two",
                "priority": MessagePriority.NORMAL,
                "idempotency_key": uuid4(),
            },
        ],
    )

    usecase.create_message(single)
    usecase.batch_create_message(batch)

    text = metrics_text()
    assert 'abrio_messages_submitted_total{priority="normal",submission_type="single"} 1.0' in text
    assert 'abrio_messages_submitted_total{priority="normal",submission_type="batch"} 2.0' in text
    assert 'abrio_messages_submitted_total{priority="express",submission_type="batch"} 1.0' in text


def test_idempotent_replay_does_not_increment_accepted_messages(
    message_request_factory: MessageRequestFactory,
) -> None:
    usecase = build_message_usecase(UniqueViolationRepository())
    payload = message_request_factory()

    usecase.create_message(payload)

    text = metrics_text()
    assert 'abrio_idempotent_replays_total{submission_type="single"} 1.0' in text
    assert 'abrio_messages_submitted_total{priority="normal",submission_type="single"}' not in text


def test_relay_publication_success_and_failure_metrics() -> None:
    record_relay_publish(priority=MessagePriority.EXPRESS, count=3)
    record_relay_publish_failure(priority=MessagePriority.EXPRESS, count=2)

    text = metrics_text()

    assert 'abrio_relay_jobs_published_total{priority="express"} 3.0' in text
    assert 'abrio_relay_publish_failures_total{priority="express"} 2.0' in text


def test_dispatch_backlog_and_payment_consistency_queries(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = seed_user("observability", credits=10, reserved_credits=5)
    created_at = datetime.now(UTC) - timedelta(seconds=30)
    with db_session_factory() as session:
        message = Message(
            user_id=user_id,
            recipient="+989121234567",
            body="hello",
            cost=Decimal("1.00"),
            priority=MessagePriority.EXPRESS,
            idempotency_key=uuid4(),
            payment_status=PaymentStatus.DEDUCTED,
        )
        session.add(message)
        session.flush()
        session.add(
            DispatchJob(
                message_id=message.id,
                user_id=user_id,
                payload={},
                priority=MessagePriority.EXPRESS,
                status=DispatchJobStatus.PENDING,
                available_at=created_at,
                created_at=created_at,
            )
        )
        session.commit()

    monkeypatch.setattr("core.metrics.SessionLocal", db_session_factory)

    collect_database_metrics(force=True)

    text = metrics_text()
    assert 'abrio_dispatch_ready_jobs{priority="express"} 1.0' in text
    assert 'abrio_payment_consistency_violations{type="reserved_without_message"} 1.0' in text
    assert 'abrio_payment_consistency_violations{type="message_balance_mismatch"} 1.0' in text


def test_sensitive_values_do_not_appear_in_submission_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level("INFO")
    usecase = build_message_usecase(FakeMessageRepository())
    sensitive_idempotency_key = uuid4()
    payload = MessageRequest(
        user_id=1,
        recipient="+989121234567",
        body="OTP 123456",
        priority=MessagePriority.NORMAL,
        idempotency_key=sensitive_idempotency_key,
    )

    usecase.create_message(payload)

    assert "OTP 123456" not in caplog.text
    assert "+989121234567" not in caplog.text
    assert str(sensitive_idempotency_key) not in caplog.text

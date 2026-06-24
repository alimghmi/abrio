from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import TracebackType
from typing import Any, cast
from uuid import uuid4

import pytest
from prometheus_client import generate_latest
from sqlalchemy.orm import Session

from app.usecases.dispatch import DispatchUseCase
from core.metrics import REGISTRY, reset_metrics_for_tests
from domain.enums import (
    DispatchJobStatus,
    MessagePriority,
    MessageStatus,
    PaymentStatus,
)
from infra.db.repositories.dispatch_jobs import DispatchJobRepository
from infra.db.repositories.messages import MessageRepository
from infra.providers.types import ProviderOutcome, ProviderResult

pytestmark = pytest.mark.unit


class FakeJob:
    def __init__(
        self,
        *,
        status: DispatchJobStatus = DispatchJobStatus.PUBLISHED,
        delivery_attempts: int = 0,
        retry_count: int = 0,
        locked_at: datetime | None = None,
        locked_by: str | None = None,
        priority: MessagePriority = MessagePriority.NORMAL,
    ) -> None:
        self.id = uuid4()
        self.message_id = uuid4()
        self.status = status
        self.delivery_attempts = delivery_attempts
        self.retry_count = retry_count
        self.locked_at = locked_at
        self.locked_by = locked_by
        self.priority = priority
        self.provider_message_id: str | None = None
        self.completed_at: datetime | None = None
        self.available_at: datetime | None = None
        self.last_error: str | None = None


class FakeMessage:
    def __init__(
        self,
        *,
        priority: MessagePriority = MessagePriority.NORMAL,
        payment_status: PaymentStatus = PaymentStatus.RESERVED,
        status: MessageStatus = MessageStatus.QUEUED,
        created_at: datetime | None = None,
        cost: Decimal = Decimal("1.00"),
        user_id: int = 1,
    ) -> None:
        self.id = uuid4()
        self.user_id = user_id
        self.priority = priority
        self.payment_status = payment_status
        self.status = status
        self.cost = cost
        self.recipient = "+989121234567"
        self.body = "hello"
        self.created_at = created_at or datetime.now(UTC)


class StubJobRepo(DispatchJobRepository):
    """Real mutation logic (mark_*), but reads return a fixed fake job."""

    def __init__(self, job: FakeJob) -> None:
        self._job = job

    def get_for_update(self, job_id: Any) -> Any:  # noqa: ARG002
        return self._job


class StubMessageRepo(MessageRepository):
    def __init__(self, message: FakeMessage) -> None:
        self._message = message

    def get_for_update(self, message_id: Any) -> Any:  # noqa: ARG002
        return self._message


class StubBalanceRepo:
    def __init__(self) -> None:
        self.settled: list[tuple[int, Decimal]] = []
        self.released: list[tuple[int, Decimal]] = []

    def settle(self, *, user_id: int, amount: Decimal) -> None:
        self.settled.append((user_id, amount))

    def release_credits(self, *, user_id: int, amount: Decimal) -> None:
        self.released.append((user_id, amount))


class StubProvider:
    def __init__(self, result: ProviderResult) -> None:
        self.result = result
        self.calls = 0

    def send(self, *, message_id: Any, recipient: str, body: str) -> ProviderResult:  # noqa: ARG002
        self.calls += 1
        return self.result


class FakeTransaction:
    def __enter__(self) -> "FakeTransaction":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return False


class FakeSession:
    def begin(self) -> FakeTransaction:
        return FakeTransaction()


def build_usecase(
    *,
    job: FakeJob,
    message: FakeMessage,
    result: ProviderResult,
    max_delivery_attempts: int = 5,
    express_ttl_seconds: int | None = None,
) -> tuple[DispatchUseCase, StubBalanceRepo, StubProvider]:
    provider = StubProvider(result)
    usecase = DispatchUseCase(
        session=cast(Session, FakeSession()),
        provider=cast(Any, provider),
        max_delivery_attempts=max_delivery_attempts,
        express_ttl_seconds=express_ttl_seconds,
    )
    balance_repo = StubBalanceRepo()
    usecase._job_repo = StubJobRepo(job)
    usecase._message_repo = StubMessageRepo(message)
    usecase._balance_repo = cast(Any, balance_repo)
    return usecase, balance_repo, provider


def test_success_settles_and_completes() -> None:
    job = FakeJob(status=DispatchJobStatus.PUBLISHED)
    message = FakeMessage(cost=Decimal("2.50"), user_id=7)
    result = ProviderResult(outcome=ProviderOutcome.SUCCESS, provider_message_id="prov-1")
    usecase, balance_repo, provider = build_usecase(job=job, message=message, result=result)

    usecase.process(job.id)

    assert job.status == DispatchJobStatus.COMPLETED
    assert job.provider_message_id == "prov-1"
    assert message.status == MessageStatus.SENT
    assert message.payment_status == PaymentStatus.DEDUCTED
    assert balance_repo.settled == [(7, Decimal("2.50"))]
    assert balance_repo.released == []
    assert provider.calls == 1


def test_temporary_failure_schedules_retry_and_keeps_reservation() -> None:
    job = FakeJob(status=DispatchJobStatus.PUBLISHED, delivery_attempts=0)
    message = FakeMessage(user_id=3)
    result = ProviderResult(outcome=ProviderOutcome.TEMPORARY_FAILURE, error="timeout")
    usecase, balance_repo, _ = build_usecase(job=job, message=message, result=result)

    usecase.process(job.id)

    assert job.status == DispatchJobStatus.RETRY
    assert job.delivery_attempts == 1
    assert message.status == MessageStatus.FAILED
    assert message.payment_status == PaymentStatus.RESERVED
    assert balance_repo.settled == []
    assert balance_repo.released == []


def test_permanent_failure_refunds_reservation() -> None:
    job = FakeJob(status=DispatchJobStatus.PUBLISHED)
    message = FakeMessage(cost=Decimal("1.00"), user_id=9)
    result = ProviderResult(outcome=ProviderOutcome.PERMANENT_FAILURE, error="invalid recipient")
    usecase, balance_repo, _ = build_usecase(job=job, message=message, result=result)

    usecase.process(job.id)

    assert job.status == DispatchJobStatus.FAILED
    assert message.status == MessageStatus.PERMANENT_FAILED
    assert message.payment_status == PaymentStatus.REFUNDED
    assert balance_repo.released == [(9, Decimal("1.00"))]
    assert balance_repo.settled == []


def test_retry_exhaustion_becomes_permanent_failure() -> None:
    # delivery_attempts=4, next attempt = 5 == max -> exhausted.
    job = FakeJob(status=DispatchJobStatus.PUBLISHED, delivery_attempts=4)
    message = FakeMessage(user_id=2)
    result = ProviderResult(outcome=ProviderOutcome.TEMPORARY_FAILURE, error="still failing")
    usecase, balance_repo, _ = build_usecase(
        job=job, message=message, result=result, max_delivery_attempts=5
    )

    usecase.process(job.id)

    assert job.status == DispatchJobStatus.FAILED
    assert message.payment_status == PaymentStatus.REFUNDED
    assert balance_repo.released == [(2, Decimal("1.00"))]


def test_publish_attempts_do_not_consume_delivery_budget() -> None:
    # A job that hit publish retries (retry_count high) still gets its full
    # delivery budget because the counters are separate.
    job = FakeJob(status=DispatchJobStatus.PUBLISHED, retry_count=10, delivery_attempts=0)
    message = FakeMessage(user_id=1)
    result = ProviderResult(outcome=ProviderOutcome.TEMPORARY_FAILURE, error="timeout")
    usecase, _, _ = build_usecase(job=job, message=message, result=result, max_delivery_attempts=5)

    usecase.process(job.id)

    assert job.status == DispatchJobStatus.RETRY
    assert job.delivery_attempts == 1


def test_expired_express_message_is_failed_before_send() -> None:
    job = FakeJob(status=DispatchJobStatus.PUBLISHED, priority=MessagePriority.EXPRESS)
    message = FakeMessage(
        priority=MessagePriority.EXPRESS,
        created_at=datetime.now(UTC) - timedelta(seconds=300),
        user_id=5,
    )
    result = ProviderResult(outcome=ProviderOutcome.SUCCESS, provider_message_id="should-not-send")
    usecase, balance_repo, provider = build_usecase(
        job=job, message=message, result=result, express_ttl_seconds=120
    )

    usecase.process(job.id)

    assert provider.calls == 0
    assert job.status == DispatchJobStatus.FAILED
    assert message.payment_status == PaymentStatus.REFUNDED
    assert balance_repo.released == [(5, Decimal("1.00"))]


def test_terminal_job_ignores_duplicate_delivery() -> None:
    reset_metrics_for_tests()
    job = FakeJob(status=DispatchJobStatus.COMPLETED)
    message = FakeMessage()
    result = ProviderResult(outcome=ProviderOutcome.SUCCESS, provider_message_id="x")
    usecase, balance_repo, provider = build_usecase(job=job, message=message, result=result)

    usecase.process(job.id)

    assert provider.calls == 0
    assert job.status == DispatchJobStatus.COMPLETED
    assert balance_repo.settled == []
    assert "abrio_delivery_attempts_total{" not in generate_latest(REGISTRY).decode("utf-8")


def test_in_flight_duplicate_is_skipped() -> None:
    # A job already DISPATCHING with a fresh lease held by another worker must
    # not be delivered again by a concurrent duplicate.
    job = FakeJob(
        status=DispatchJobStatus.DISPATCHING,
        locked_at=datetime.now(UTC),
        locked_by="other-worker",
    )
    message = FakeMessage()
    result = ProviderResult(outcome=ProviderOutcome.SUCCESS, provider_message_id="x")
    usecase, balance_repo, provider = build_usecase(job=job, message=message, result=result)

    usecase.process(job.id)

    assert provider.calls == 0
    assert job.status == DispatchJobStatus.DISPATCHING
    assert job.locked_by == "other-worker"
    assert balance_repo.settled == []


def test_stale_dispatching_lease_is_reclaimed_and_delivered() -> None:
    # A job DISPATCHING with a stale lease (previous worker died) is reclaimed
    # and delivered by the next worker.
    job = FakeJob(
        status=DispatchJobStatus.DISPATCHING,
        locked_at=datetime.now(UTC) - timedelta(seconds=600),
        locked_by="dead-worker",
    )
    message = FakeMessage(user_id=8)
    result = ProviderResult(outcome=ProviderOutcome.SUCCESS, provider_message_id="prov-2")
    usecase, balance_repo, provider = build_usecase(job=job, message=message, result=result)

    usecase.process(job.id)

    assert provider.calls == 1
    assert job.status == DispatchJobStatus.COMPLETED
    assert balance_repo.settled == [(8, Decimal("1.00"))]


def test_publish_in_progress_job_is_processable() -> None:
    # Worker received the task before the relay committed the PUBLISHED mark.
    job = FakeJob(
        status=DispatchJobStatus.PENDING,
        locked_at=datetime.now(UTC),
        locked_by="relay-1",
    )
    message = FakeMessage(user_id=4)
    result = ProviderResult(outcome=ProviderOutcome.SUCCESS, provider_message_id="prov-9")
    usecase, balance_repo, provider = build_usecase(job=job, message=message, result=result)

    usecase.process(job.id)

    assert provider.calls == 1
    assert job.status == DispatchJobStatus.COMPLETED
    assert balance_repo.settled == [(4, Decimal("1.00"))]

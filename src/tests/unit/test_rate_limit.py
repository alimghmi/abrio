from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence

import pytest
from redis.exceptions import RedisError
from starlette.types import Message, Receive, Scope, Send

from api.rate_limit import (
    RATE_LIMIT_UNAVAILABLE_CODE,
    RATE_LIMITED_CODE,
    RateLimitDecision,
    RateLimiter,
    RateLimitMiddleware,
    ResolvedRateLimit,
    resolve_rate_limits,
)
from core.config import Settings

pytestmark = pytest.mark.unit


class FakeLimiter:
    def __init__(self, decision: RateLimitDecision):
        self.decision = decision
        self.calls: list[list[ResolvedRateLimit]] = []

    async def check(self, buckets: Sequence[ResolvedRateLimit]) -> RateLimitDecision:
        self.calls.append(list(buckets))
        return self.decision


class FailingLimiter:
    def __init__(self) -> None:
        self.calls = 0

    async def check(self, _buckets: Sequence[ResolvedRateLimit]) -> RateLimitDecision:
        self.calls += 1
        raise RedisError("redis unavailable")


class UnexpectedLimiter:
    async def check(self, _buckets: Sequence[ResolvedRateLimit]) -> RateLimitDecision:
        raise AssertionError("limiter should not be called")


async def echo_app(scope: Scope, receive: Receive, send: Send) -> None:
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break

    payload = json.loads(body.decode("utf-8"))
    response_body = json.dumps({"body": payload["body"]}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": response_body})


def make_scope(
    method: str,
    path: str,
    query_string: bytes = b"",
    client: tuple[str, int] = ("127.0.0.1", 1234),
) -> Scope:
    return {
        "type": "http",
        "method": method,
        "path": path,
        "query_string": query_string,
        "client": client,
    }


def run_middleware_request(
    settings: Settings,
    limiter: RateLimiter,
    body: dict[str, object],
) -> tuple[int, dict[str, str], dict[str, object]]:
    response_messages: list[Message] = []
    request_sent = False
    middleware = RateLimitMiddleware(echo_app, settings=settings, limiter=limiter)
    request_body = json.dumps(body).encode("utf-8")

    async def receive() -> Message:
        nonlocal request_sent
        if request_sent:
            return {"type": "http.request", "body": b"", "more_body": False}

        request_sent = True
        return {"type": "http.request", "body": request_body, "more_body": False}

    async def send(message: Message) -> None:
        response_messages.append(message)

    asyncio.run(
        middleware(
            make_scope("POST", "/api/v1/messages/"),
            receive,
            send,
        )
    )

    start = next(
        message for message in response_messages if message["type"] == "http.response.start"
    )
    response_body = b"".join(
        message.get("body", b"")
        for message in response_messages
        if message["type"] == "http.response.body"
    )
    headers = {
        key.decode("latin-1"): value.decode("latin-1") for key, value in start.get("headers", [])
    }
    return int(start["status"]), headers, json.loads(response_body.decode("utf-8"))


def error_code(response: dict[str, object]) -> object:
    error = response["error"]
    assert isinstance(error, dict)
    return error["code"]


def test_single_message_limit_is_keyed_by_user_and_priority() -> None:
    settings = Settings(rate_limit_enabled=True)
    body = b'{"user_id": 42, "priority": "express"}'

    buckets = resolve_rate_limits(make_scope("POST", "/api/v1/messages/"), settings, body)

    assert [(bucket.rule.name, bucket.identity, bucket.cost) for bucket in buckets] == [
        ("messages:express", "user:42", 1),
        ("global:messages:express", "global", 1),
        ("system:messages", "system", 1),
        ("system:requests", "system", 1),
    ]


def test_batch_message_limit_charges_per_message_priority_and_request() -> None:
    settings = Settings(rate_limit_enabled=True)
    body = (
        b'{"user_id": 9, "messages": ['
        b'{"priority": "normal"}, {"priority": "express"}, {"priority": "normal"}'
        b"]}"
    )

    buckets = resolve_rate_limits(make_scope("POST", "/api/v1/messages/batch"), settings, body)

    assert [(bucket.rule.name, bucket.identity, bucket.cost) for bucket in buckets] == [
        ("messages:batch_requests", "user:9", 1),
        ("global:messages:batch_requests", "global", 1),
        ("messages:normal", "user:9", 2),
        ("global:messages:normal", "global", 2),
        ("messages:express", "user:9", 1),
        ("global:messages:express", "global", 1),
        ("system:messages", "system", 3),
        ("system:requests", "system", 1),
    ]


def test_health_routes_are_not_rate_limited() -> None:
    settings = Settings(rate_limit_enabled=True)

    buckets = resolve_rate_limits(make_scope("GET", "/api/v1/health/ready"), settings)

    assert buckets == []


@pytest.mark.parametrize(
    ("method", "path", "query_string", "expected_buckets"),
    [
        (
            "GET",
            "/api/v1/messages",
            b"user_id=5",
            [("messages:reports", "user:5"), ("global:messages:reports", "global")],
        ),
        (
            "GET",
            "/api/v1/messages/abc",
            b"user_id=5",
            [("messages:status", "user:5"), ("global:messages:status", "global")],
        ),
        (
            "GET",
            "/api/v1/messages",
            b"",
            [("messages:reports", "ip:127.0.0.1"), ("global:messages:reports", "global")],
        ),
        (
            "POST",
            "/api/v1/users",
            b"",
            [("users:creates", "ip:127.0.0.1"), ("global:users:creates", "global")],
        ),
        (
            "POST",
            "/api/v1/users/5/topup",
            b"",
            [("users:writes", "user:5"), ("global:users:writes", "global")],
        ),
        (
            "GET",
            "/api/v1/users/5",
            b"",
            [("users:reads", "ip:127.0.0.1"), ("global:users:reads", "global")],
        ),
        (
            "GET",
            "/api/v1/pricing",
            b"",
            [("pricing", "ip:127.0.0.1"), ("global:pricing", "global")],
        ),
    ],
)
def test_non_submission_endpoint_policies(
    method: str,
    path: str,
    query_string: bytes,
    expected_buckets: list[tuple[str, str]],
) -> None:
    settings = Settings(rate_limit_enabled=True)

    buckets = resolve_rate_limits(make_scope(method, path, query_string), settings)

    assert [(bucket.rule.name, bucket.identity, bucket.cost) for bucket in buckets] == [
        *[(name, identity, 1) for name, identity in expected_buckets],
        ("system:requests", "system", 1),
    ]


def test_unknown_api_route_is_still_system_limited() -> None:
    settings = Settings(rate_limit_enabled=True)

    buckets = resolve_rate_limits(make_scope("GET", "/api/v1/unknown"), settings)

    assert [(bucket.rule.name, bucket.identity, bucket.cost) for bucket in buckets] == [
        ("system:requests", "system", 1)
    ]


def test_disabled_middleware_does_not_call_limiter_and_replays_nothing() -> None:
    settings = Settings(rate_limit_enabled=False)
    status_code, _headers, response = run_middleware_request(
        settings,
        UnexpectedLimiter(),
        {"user_id": 1, "priority": "normal", "body": "hello"},
    )

    assert status_code == 200
    assert response == {"body": "hello"}


def test_allowed_middleware_replays_body_to_fastapi() -> None:
    settings = Settings(rate_limit_enabled=True)
    limiter = FakeLimiter(
        RateLimitDecision(allowed=True, limit=1200, remaining=199, retry_after_seconds=0)
    )
    status_code, _headers, response = run_middleware_request(
        settings,
        limiter,
        {"user_id": 1, "priority": "normal", "body": "hello"},
    )

    assert status_code == 200
    assert response == {"body": "hello"}
    assert len(limiter.calls) == 1
    assert limiter.calls[0][0].identity == "user:1"
    assert [(bucket.rule.name, bucket.identity) for bucket in limiter.calls[0]] == [
        ("messages:normal", "user:1"),
        ("global:messages:normal", "global"),
        ("system:messages", "system"),
        ("system:requests", "system"),
    ]


def test_rejected_middleware_returns_429_with_rate_limit_headers() -> None:
    settings = Settings(rate_limit_enabled=True)
    limiter = FakeLimiter(
        RateLimitDecision(allowed=False, limit=300, remaining=0, retry_after_seconds=4)
    )
    status_code, headers, response = run_middleware_request(
        settings,
        limiter,
        {"user_id": 1, "priority": "express", "body": "hello"},
    )

    assert status_code == 429
    assert error_code(response) == RATE_LIMITED_CODE
    assert headers["retry-after"] == "4"
    assert headers["x-ratelimit-limit"] == "300"
    assert headers["x-ratelimit-remaining"] == "0"


def test_redis_failure_can_fail_closed() -> None:
    settings = Settings(rate_limit_enabled=True, rate_limit_fail_open=False)
    status_code, _headers, response = run_middleware_request(
        settings,
        FailingLimiter(),
        {"user_id": 1, "priority": "normal", "body": "hello"},
    )

    assert status_code == 503
    assert error_code(response) == RATE_LIMIT_UNAVAILABLE_CODE

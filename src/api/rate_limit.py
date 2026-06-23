from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs

from fastapi import status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from core.config import Settings
from core.logging import get_logger
from core.metrics import record_submission_rejected
from domain.enums import MessagePriority

RATE_LIMITED_CODE = "rate_limited"
RATE_LIMIT_UNAVAILABLE_CODE = "rate_limit_unavailable"
WINDOW_SECONDS = 60
GLOBAL_IDENTITY = "global"
SYSTEM_IDENTITY = "system"

_TOKEN_BUCKET_SCRIPT = """
local now = tonumber(ARGV[1])
local bucket_count = tonumber(ARGV[2])
local denied = 0
local denied_index = 0
local retry_after = 0
local min_remaining = nil
local states = {}

for i = 1, bucket_count do
    local arg_index = 3 + ((i - 1) * 4)
    local rate = tonumber(ARGV[arg_index])
    local capacity = tonumber(ARGV[arg_index + 1])
    local cost = tonumber(ARGV[arg_index + 2])
    local ttl_ms = tonumber(ARGV[arg_index + 3])

    local bucket = redis.call("HMGET", KEYS[i], "tokens", "updated_at")
    local tokens = tonumber(bucket[1])
    local updated_at = tonumber(bucket[2])

    if tokens == nil then
        tokens = capacity
    end

    if updated_at == nil then
        updated_at = now
    end

    local elapsed = math.max(0, now - updated_at)
    tokens = math.min(capacity, tokens + (elapsed * rate))

    local remaining = tokens - cost
    if tokens < cost then
        denied = 1
        remaining = tokens
        local bucket_retry_after = math.ceil((cost - tokens) / rate)
        if bucket_retry_after > retry_after then
            retry_after = bucket_retry_after
            denied_index = i
        end
    end

    if min_remaining == nil or remaining < min_remaining then
        min_remaining = remaining
    end

    states[i] = {tokens, cost, ttl_ms}
end

for i = 1, bucket_count do
    local tokens = states[i][1]
    local cost = states[i][2]
    local ttl_ms = states[i][3]

    if denied == 0 then
        tokens = tokens - cost
    end

    redis.call("HSET", KEYS[i], "tokens", tokens, "updated_at", now)
    redis.call("PEXPIRE", KEYS[i], ttl_ms)
end

return {1 - denied, math.floor(min_remaining), retry_after, denied_index}
"""


@dataclass(frozen=True)
class RateLimitRule:
    name: str
    limit: int
    window_seconds: int
    burst: int

    @property
    def refill_rate_per_second(self) -> float:
        return self.limit / self.window_seconds

    @property
    def ttl_milliseconds(self) -> int:
        return self.window_seconds * 2 * 1000


@dataclass(frozen=True)
class ResolvedRateLimit:
    rule: RateLimitRule
    identity: str
    cost: int = 1


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    limit: int
    remaining: int
    retry_after_seconds: int
    denied_rule_name: str | None = None


class RateLimiter(Protocol):
    async def check(self, buckets: Sequence[ResolvedRateLimit]) -> RateLimitDecision:  # type: ignore
        pass


class RedisTokenBucketRateLimiter:
    def __init__(self, redis_client: Redis, key_prefix: str):
        self.redis = redis_client
        self.key_prefix = key_prefix.strip(":")

    async def check(self, buckets: Sequence[ResolvedRateLimit]) -> RateLimitDecision:
        if not buckets:
            return RateLimitDecision(allowed=True, limit=0, remaining=0, retry_after_seconds=0)

        keys = [self._key_for(bucket) for bucket in buckets]
        args: list[str] = [str(time.time()), str(len(buckets))]
        for bucket in buckets:
            args.extend(
                [
                    str(bucket.rule.refill_rate_per_second),
                    str(bucket.rule.burst),
                    str(bucket.cost),
                    str(bucket.rule.ttl_milliseconds),
                ]
            )

        result = await self.redis.eval(_TOKEN_BUCKET_SCRIPT, len(keys), *keys, *args)
        allowed = bool(int(result[0]))
        remaining = max(0, int(result[1]))
        retry_after_seconds = max(0, int(result[2]))
        denied_index = int(result[3]) - 1

        header_bucket = buckets[denied_index] if denied_index >= 0 else buckets[0]
        return RateLimitDecision(
            allowed=allowed,
            limit=header_bucket.rule.limit,
            remaining=remaining,
            retry_after_seconds=retry_after_seconds,
            denied_rule_name=None if allowed else header_bucket.rule.name,
        )

    def _key_for(self, bucket: ResolvedRateLimit) -> str:
        return f"{self.key_prefix}:{bucket.rule.name}:{bucket.identity}"


class RateLimitMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings, limiter: RateLimiter):
        self.app = app
        self.settings = settings
        self.limiter = limiter
        self.logger = get_logger(__name__)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self.settings.rate_limit_enabled:
            await self.app(scope, receive, send)
            return

        request_receive = receive
        body = b""
        if request_may_need_body(scope, self.settings):
            body = await read_body(receive)
            request_receive = replay_body(body)

        buckets = resolve_rate_limits(scope, self.settings, body)
        if not buckets:
            await self.app(scope, request_receive, send)
            return

        try:
            decision = await self.limiter.check(buckets)
        except RedisError as exc:
            self.logger.warning(
                "dependency_unavailable",
                extra={
                    "dependency": "redis",
                    "operation": "rate_limit_check",
                    "error_type": type(exc).__name__,
                    "fail_open": self.settings.rate_limit_fail_open,
                },
                exc_info=True,
            )
            if self.settings.rate_limit_fail_open:
                await self.app(scope, request_receive, send)
                return

            response = rate_limit_unavailable_response()
            record_rate_limit_rejection(scope, self.settings, "system_rate_limited")
            await response(scope, request_receive, send)
            return

        if not decision.allowed:
            reason = rate_limit_rejection_reason(decision.denied_rule_name)
            record_rate_limit_rejection(scope, self.settings, reason)
            self.logger.info(
                "rate_limit_rejected",
                extra={
                    "reason": reason,
                    "limit": decision.limit,
                    "retry_after_seconds": decision.retry_after_seconds,
                },
            )
            response = rate_limited_response(decision)
            await response(scope, request_receive, send)
            return

        await self.app(scope, request_receive, send)


def build_redis_rate_limiter(settings: Settings) -> RedisTokenBucketRateLimiter:
    redis_url = settings.rate_limit_redis_url or settings.redis_url
    redis_client = Redis.from_url(redis_url, encoding="utf-8", decode_responses=True)
    return RedisTokenBucketRateLimiter(
        redis_client=redis_client,
        key_prefix=settings.rate_limit_key_prefix,
    )


def resolve_rate_limits(
    scope: Scope,
    settings: Settings,
    body: bytes = b"",
) -> list[ResolvedRateLimit]:
    method = str(scope.get("method", "")).upper()
    relative_path = relative_api_path(scope, settings)
    if relative_path is None:
        return []

    if relative_path.startswith("/health/"):
        return []

    payload = parse_json_body(body) if body else {}
    client_identity = get_client_identity(scope)

    if method == "POST" and relative_path == "/messages":
        return resolve_single_message_limits(settings, payload, client_identity)

    if method == "POST" and relative_path == "/messages/batch":
        return resolve_batch_message_limits(settings, payload, client_identity)

    if method == "GET" and relative_path == "/messages/summary":
        identity = user_identity_from_query(scope) or client_identity
        return endpoint_limits(
            per_identity_rule=message_reports_rule(settings),
            identity=identity,
            global_rule=global_message_reports_rule(settings),
            settings=settings,
        )

    if method == "GET" and relative_path == "/messages":
        identity = user_identity_from_query(scope) or client_identity
        return endpoint_limits(
            per_identity_rule=message_reports_rule(settings),
            identity=identity,
            global_rule=global_message_reports_rule(settings),
            settings=settings,
        )

    if method == "GET" and relative_path.startswith("/messages/"):
        identity = user_identity_from_query(scope) or client_identity
        return endpoint_limits(
            per_identity_rule=message_status_rule(settings),
            identity=identity,
            global_rule=global_message_status_rule(settings),
            settings=settings,
        )

    if method == "POST" and relative_path == "/users":
        return endpoint_limits(
            per_identity_rule=user_creates_rule(settings),
            identity=client_identity,
            global_rule=global_user_creates_rule(settings),
            settings=settings,
        )

    if method == "GET" and (relative_path == "/users" or relative_path.startswith("/users/")):
        return endpoint_limits(
            per_identity_rule=user_reads_rule(settings),
            identity=client_identity,
            global_rule=global_user_reads_rule(settings),
            settings=settings,
        )

    if method == "POST" and relative_path.startswith("/users/"):
        identity = user_identity_from_user_path(relative_path) or client_identity
        return endpoint_limits(
            per_identity_rule=user_writes_rule(settings),
            identity=identity,
            global_rule=global_user_writes_rule(settings),
            settings=settings,
        )

    if method == "GET" and relative_path == "/pricing":
        return endpoint_limits(
            per_identity_rule=pricing_rule(settings),
            identity=client_identity,
            global_rule=global_pricing_rule(settings),
            settings=settings,
        )

    return system_request_limits(settings)


def record_rate_limit_rejection(scope: Scope, settings: Settings, reason: str) -> None:
    submission_type = submission_type_for_scope(scope, settings)
    if submission_type is None:
        return

    record_submission_rejected(reason=reason, submission_type=submission_type)


def submission_type_for_scope(scope: Scope, settings: Settings) -> str | None:
    method = str(scope.get("method", "")).upper()
    relative_path = relative_api_path(scope, settings)
    if method == "POST" and relative_path == "/messages":
        return "single"
    if method == "POST" and relative_path == "/messages/batch":
        return "batch"
    return None


def rate_limit_rejection_reason(denied_rule_name: str | None) -> str:
    if denied_rule_name is None:
        return "rate_limited"
    if denied_rule_name.startswith("global:"):
        return "global_rate_limited"
    if denied_rule_name.startswith("system:"):
        return "system_rate_limited"
    return "rate_limited"


def resolve_single_message_limits(
    settings: Settings,
    payload: dict[str, object],
    fallback_identity: str,
) -> list[ResolvedRateLimit]:
    identity = user_identity_from_payload(payload) or fallback_identity
    priority = payload.get("priority")
    return message_limits(
        settings=settings,
        priority=priority,
        identity=identity,
        cost=1,
        include_system_request=True,
    )


def resolve_batch_message_limits(
    settings: Settings,
    payload: dict[str, object],
    fallback_identity: str,
) -> list[ResolvedRateLimit]:
    identity = user_identity_from_payload(payload) or fallback_identity
    normal_count, express_count = count_batch_priorities(payload.get("messages"))
    total_count = normal_count + express_count
    limits = [
        ResolvedRateLimit(batch_requests_rule(settings), identity),
        ResolvedRateLimit(global_batch_requests_rule(settings), GLOBAL_IDENTITY),
    ]

    if normal_count:
        limits.extend(
            message_limits(
                settings=settings,
                priority=MessagePriority.NORMAL.value,
                identity=identity,
                cost=normal_count,
                include_system_request=False,
                include_system_messages=False,
            )
        )

    if express_count:
        limits.extend(
            message_limits(
                settings=settings,
                priority=MessagePriority.EXPRESS.value,
                identity=identity,
                cost=express_count,
                include_system_request=False,
                include_system_messages=False,
            )
        )

    if total_count:
        limits.append(
            ResolvedRateLimit(system_messages_rule(settings), SYSTEM_IDENTITY, total_count)
        )

    return limits + system_request_limits(settings)


def endpoint_limits(
    *,
    per_identity_rule: RateLimitRule,
    identity: str,
    global_rule: RateLimitRule,
    settings: Settings,
) -> list[ResolvedRateLimit]:
    return [
        ResolvedRateLimit(per_identity_rule, identity),
        ResolvedRateLimit(global_rule, GLOBAL_IDENTITY),
        *system_request_limits(settings),
    ]


def message_limits(
    *,
    settings: Settings,
    priority: object,
    identity: str,
    cost: int,
    include_system_request: bool,
    include_system_messages: bool = True,
) -> list[ResolvedRateLimit]:
    limits = [
        ResolvedRateLimit(message_submission_rule(settings, priority), identity, cost),
        ResolvedRateLimit(
            global_message_submission_rule(settings, priority), GLOBAL_IDENTITY, cost
        ),
    ]

    if include_system_messages:
        limits.append(ResolvedRateLimit(system_messages_rule(settings), SYSTEM_IDENTITY, cost))

    if include_system_request:
        limits.extend(system_request_limits(settings))

    return limits


def system_request_limits(settings: Settings) -> list[ResolvedRateLimit]:
    return [ResolvedRateLimit(system_requests_rule(settings), SYSTEM_IDENTITY)]


def request_may_need_body(scope: Scope, settings: Settings) -> bool:
    method = str(scope.get("method", "")).upper()
    if method != "POST":
        return False

    relative_path = relative_api_path(scope, settings)
    return relative_path in {"/messages", "/messages/batch"}


async def read_body(receive: Receive) -> bytes:
    body = b""
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return body

        chunk = message.get("body", b"")
        if isinstance(chunk, bytes):
            body += chunk
        if not message.get("more_body", False):
            return body


def replay_body(body: bytes) -> Receive:
    body_sent = False

    async def receive() -> Message:
        nonlocal body_sent
        if body_sent:
            return {"type": "http.request", "body": b"", "more_body": False}

        body_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return receive


def relative_api_path(scope: Scope, settings: Settings) -> str | None:
    path = str(scope.get("path", ""))
    api_base = f"{settings.api_prefix.rstrip('/')}/v1"

    if path != api_base and not path.startswith(f"{api_base}/"):
        return None

    relative_path = path[len(api_base) :] or "/"
    return relative_path.rstrip("/") or "/"


def parse_json_body(body: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(body.decode("utf-8")) if body else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}

    return parsed if isinstance(parsed, dict) else {}


def count_batch_priorities(messages: object) -> tuple[int, int]:
    if not isinstance(messages, list):
        return 0, 0

    normal_count = 0
    express_count = 0
    for message in messages:
        if not isinstance(message, dict):
            normal_count += 1
            continue

        if message.get("priority") == MessagePriority.EXPRESS.value:
            express_count += 1
        else:
            normal_count += 1

    return normal_count, express_count


def get_client_identity(scope: Scope) -> str:
    client = scope.get("client")
    if isinstance(client, tuple) and client:
        return f"ip:{client[0]}"

    return "ip:unknown"


def user_identity_from_payload(payload: dict[str, object]) -> str | None:
    user_id = payload.get("user_id")
    if isinstance(user_id, bool):
        return None

    if isinstance(user_id, int):
        return f"user:{user_id}"

    if isinstance(user_id, str) and user_id.isdecimal():
        return f"user:{user_id}"

    return None


def user_identity_from_query(scope: Scope) -> str | None:
    query_string = scope.get("query_string", b"")
    if not isinstance(query_string, bytes):
        return None

    params = parse_qs(query_string.decode("latin-1"))
    values = params.get("user_id")
    if not values:
        return None

    user_id = values[0]
    return f"user:{user_id}" if user_id.isdecimal() else None


def user_identity_from_user_path(relative_path: str) -> str | None:
    parts = relative_path.strip("/").split("/")
    if len(parts) < 2 or parts[0] != "users":
        return None

    return f"user:{parts[1]}" if parts[1].isdecimal() else None


def message_submission_rule(settings: Settings, priority: object) -> RateLimitRule:
    if priority == MessagePriority.EXPRESS.value:
        return RateLimitRule(
            name="messages:express",
            limit=settings.rate_limit_express_messages_per_minute,
            window_seconds=WINDOW_SECONDS,
            burst=settings.rate_limit_express_messages_burst,
        )

    return RateLimitRule(
        name="messages:normal",
        limit=settings.rate_limit_normal_messages_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_normal_messages_burst,
    )


def global_message_submission_rule(settings: Settings, priority: object) -> RateLimitRule:
    if priority == MessagePriority.EXPRESS.value:
        return RateLimitRule(
            name="global:messages:express",
            limit=settings.rate_limit_global_express_messages_per_minute,
            window_seconds=WINDOW_SECONDS,
            burst=settings.rate_limit_global_express_messages_burst,
        )

    return RateLimitRule(
        name="global:messages:normal",
        limit=settings.rate_limit_global_normal_messages_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_global_normal_messages_burst,
    )


def batch_requests_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="messages:batch_requests",
        limit=settings.rate_limit_batch_requests_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_batch_requests_burst,
    )


def global_batch_requests_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="global:messages:batch_requests",
        limit=settings.rate_limit_global_batch_requests_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_global_batch_requests_burst,
    )


def message_status_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="messages:status",
        limit=settings.rate_limit_message_status_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_message_status_burst,
    )


def global_message_status_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="global:messages:status",
        limit=settings.rate_limit_global_message_status_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_global_message_status_burst,
    )


def message_reports_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="messages:reports",
        limit=settings.rate_limit_message_reports_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_message_reports_burst,
    )


def global_message_reports_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="global:messages:reports",
        limit=settings.rate_limit_global_message_reports_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_global_message_reports_burst,
    )


def user_reads_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="users:reads",
        limit=settings.rate_limit_user_reads_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_user_reads_burst,
    )


def global_user_reads_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="global:users:reads",
        limit=settings.rate_limit_global_user_reads_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_global_user_reads_burst,
    )


def user_writes_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="users:writes",
        limit=settings.rate_limit_user_writes_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_user_writes_burst,
    )


def global_user_writes_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="global:users:writes",
        limit=settings.rate_limit_global_user_writes_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_global_user_writes_burst,
    )


def user_creates_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="users:creates",
        limit=settings.rate_limit_user_creates_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_user_creates_burst,
    )


def global_user_creates_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="global:users:creates",
        limit=settings.rate_limit_global_user_creates_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_global_user_creates_burst,
    )


def pricing_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="pricing",
        limit=settings.rate_limit_pricing_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_pricing_burst,
    )


def global_pricing_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="global:pricing",
        limit=settings.rate_limit_global_pricing_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_global_pricing_burst,
    )


def system_requests_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="system:requests",
        limit=settings.rate_limit_system_requests_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_system_requests_burst,
    )


def system_messages_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        name="system:messages",
        limit=settings.rate_limit_system_messages_per_minute,
        window_seconds=WINDOW_SECONDS,
        burst=settings.rate_limit_system_messages_burst,
    )


def rate_limited_response(decision: RateLimitDecision) -> JSONResponse:
    retry_after = max(1, math.ceil(decision.retry_after_seconds))
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content=jsonable_encoder(
            {
                "error": {
                    "code": RATE_LIMITED_CODE,
                    "message": f"Too many requests. Retry after {retry_after} seconds.",
                }
            }
        ),
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Limit": str(decision.limit),
            "X-RateLimit-Remaining": str(max(0, decision.remaining)),
            "X-RateLimit-Reset": str(retry_after),
        },
    )


def rate_limit_unavailable_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content=jsonable_encoder(
            {
                "error": {
                    "code": RATE_LIMIT_UNAVAILABLE_CODE,
                    "message": "Rate limiting is temporarily unavailable.",
                }
            }
        ),
    )

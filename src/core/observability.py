from __future__ import annotations

import time

from prometheus_client import CONTENT_TYPE_LATEST
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Scope

from core.config import Settings
from core.logging import get_logger
from core.metrics import latest_metrics, record_http_request, record_submission_rejected
from core.request_context import normalize_request_id, reset_request_id, set_request_id

logger = get_logger(__name__)

HEALTH_ROUTE_PREFIX = "/api/v1/health/"
UNKNOWN_ROUTE = "__unmatched__"


class RequestIdAndMetricsMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: Settings):
        super().__init__(app)
        self.settings = settings

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = normalize_request_id(request.headers.get(self.settings.log_request_id_header))
        token = set_request_id(request_id)
        start_time = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration_seconds = time.perf_counter() - start_time
            route = normalized_route(request.scope)
            record_http_request(
                method=request.method,
                route=route,
                status_code=500,
                duration_seconds=duration_seconds,
            )
            if not _skip_request_log(request.scope, route):
                logger.error(
                    "http_request_completed",
                    extra={
                        "method": request.method,
                        "route": route,
                        "status_code": 500,
                        "duration_ms": round(duration_seconds * 1000, 3),
                    },
                    exc_info=True,
                )
            reset_request_id(token)
            raise

        duration_seconds = time.perf_counter() - start_time
        route = normalized_route(request.scope)
        response.headers[self.settings.log_request_id_header] = request_id
        record_http_request(
            method=request.method,
            route=route,
            status_code=response.status_code,
            duration_seconds=duration_seconds,
        )
        _record_validation_rejection(request.scope, response.status_code)
        if not _skip_request_log(request.scope, route):
            logger.info(
                "http_request_completed",
                extra={
                    "method": request.method,
                    "route": route,
                    "status_code": response.status_code,
                    "duration_ms": round(duration_seconds * 1000, 3),
                },
            )
        reset_request_id(token)
        return response


async def metrics_endpoint() -> Response:
    return Response(content=latest_metrics(), media_type=CONTENT_TYPE_LATEST)


def normalized_route(scope: Scope) -> str:
    route = scope.get("route")
    path = getattr(route, "path", None)
    if path:
        return str(path)

    if scope.get("path") == "/metrics":
        return "/metrics"

    return UNKNOWN_ROUTE


def _skip_request_log(scope: Scope, route: str) -> bool:
    path = str(scope.get("path", ""))
    return route == "/metrics" or path.startswith(HEALTH_ROUTE_PREFIX)


def _record_validation_rejection(scope: Scope, status_code: int) -> None:
    if status_code != 422:
        return

    submission_type = _submission_type_for_scope(scope)
    if submission_type is None:
        return

    record_submission_rejected(reason="invalid_request", submission_type=submission_type)


def _submission_type_for_scope(scope: Scope) -> str | None:
    if scope.get("method") != "POST":
        return None

    route = normalized_route(scope)
    if route == "/api/v1/messages/":
        return "single"
    if route == "/api/v1/messages/batch":
        return "batch"
    return None

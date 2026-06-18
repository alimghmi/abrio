import re
import time

import redis
from fastapi import APIRouter
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from api.schemas.health import HealthLiveResponse, HealthReadyResponse, ServiceStatus
from core.config import get_settings
from infra.db.session import engine

settings = get_settings()
router = APIRouter()


def _get_host_and_port(redis_url: str) -> tuple:
    pattern = r"redis://(?P<host>[^:/]+):(?P<port>\d+)"
    match = re.search(pattern, redis_url)
    if match:
        return match.group("host"), int(match.group("port"))

    raise ValueError(f"Redis URL cannot be parsed: {redis_url}")


async def _check_redis(redis_url: str, timeout: float) -> ServiceStatus:
    start_time = time.time()
    try:
        host, port = _get_host_and_port(redis_url)
        r = redis.Redis(host=host, port=port, socket_connect_timeout=timeout)
        r.ping()
        r.close()
        status, error = "ok", None
    except redis.TimeoutError:
        status, error = "error", f"Redis connection timeout after {timeout}s"
    except Exception as e:
        status, error = "error", f"Redis connection failed: {e!s}"

    duration_ms = (time.time() - start_time) * 1000
    return ServiceStatus(status=status, duration_ms=duration_ms, error=error)


async def _check_db(engine) -> ServiceStatus:
    try:
        start_time = time.time()
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        status, error = "ok", None
    except SQLAlchemyError as exc:
        status, error = "error", f"Database connection failed: {exc!s}"

    duration_ms = (time.time() - start_time) * 1000
    return ServiceStatus(status=status, duration_ms=duration_ms, error=error)


@router.get("/health/live")
async def health_check_live() -> HealthLiveResponse:
    return HealthLiveResponse(status="ok", service=settings.app_name, timestamp=time.time())


@router.get("/health/ready")
async def health_check_ready() -> HealthReadyResponse:
    redis_status = await _check_redis(settings.redis_url, 1)
    db_status = await _check_db(engine)
    status = "ok" if all(_.status == "ok" for _ in [redis_status, db_status]) else "error"

    return HealthReadyResponse(
        status=status,
        service=settings.app_name,
        redis=redis_status,
        database=db_status,
        timestamp=time.time(),
    )

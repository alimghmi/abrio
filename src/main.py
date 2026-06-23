from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from api.rate_limit import RateLimitMiddleware, build_redis_rate_limiter
from api.v1.router import api_router
from core.config import get_settings
from core.logging import configure_logging, get_logger
from core.observability import RequestIdAndMetricsMiddleware, metrics_endpoint
from domain.errors import AppError


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)
    logger = get_logger(__name__)

    app = FastAPI(
        title=settings.app_name,
        debug=settings.app_debug,
    )
    app.include_router(api_router, prefix=settings.api_prefix)
    app.add_api_route("/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False)

    if settings.rate_limit_enabled:
        rate_limiter = build_redis_rate_limiter(settings)
        app.add_middleware(
            RateLimitMiddleware,
            settings=settings,
            limiter=rate_limiter,
        )
        app.router.on_shutdown.append(rate_limiter.redis.aclose)

    app.add_middleware(RequestIdAndMetricsMiddleware, settings=settings)

    logger.info(
        "application_created",
        extra={
            "api_prefix": settings.api_prefix,
            "rate_limit_enabled": settings.rate_limit_enabled,
        },
    )
    return app


app = create_app()


@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=jsonable_encoder(
            {
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                }
            }
        ),
    )

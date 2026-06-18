from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from api.v1.router import api_router
from core.config import get_settings
from core.logging import configure_logging, get_logger
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
    logger.info("application_created", extra={"api_prefix": settings.api_prefix})
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

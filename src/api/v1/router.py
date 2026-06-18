from fastapi import APIRouter

from api.routes import health, tasks

routes_router = APIRouter()
routes_router.include_router(health.router, tags=["health"])
routes_router.include_router(tasks.router, prefix="/tasks", tags=["tasks"])

api_router = APIRouter()
api_router.include_router(routes_router, prefix="/v1")

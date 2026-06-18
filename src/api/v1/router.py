from fastapi import APIRouter

from api.routes import health, users

routes_router = APIRouter()
routes_router.include_router(health.router, tags=["health"])
routes_router.include_router(users.router, prefix="/users", tags=["users"])

api_router = APIRouter()
api_router.include_router(routes_router, prefix="/v1")

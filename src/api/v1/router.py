from fastapi import APIRouter

from api.routes import health, messages, pricing, users

routes_router = APIRouter()
routes_router.include_router(health.router, tags=["health"])
routes_router.include_router(pricing.router, prefix="/pricing", tags=["pricing"])
routes_router.include_router(users.router, prefix="/users", tags=["users"])
routes_router.include_router(messages.router, prefix="/messages", tags=["messages"])

api_router = APIRouter()
api_router.include_router(routes_router, prefix="/v1")

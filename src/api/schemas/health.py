from typing import Literal

from pydantic import BaseModel


class HealthLiveResponse(BaseModel):
    status: Literal["ok"]
    service: str
    timestamp: float


class ServiceStatus(BaseModel):
    status: Literal["ok", "error"]
    duration_ms: float
    error: str | None = None


class HealthReadyResponse(BaseModel):
    status: Literal["ok", "error"]
    service: str
    redis: ServiceStatus
    database: ServiceStatus
    rabbitmq: ServiceStatus
    timestamp: float

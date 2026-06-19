import asyncio
import time

import pytest

from api.routes.health import health_check_live
from main import create_app

pytestmark = pytest.mark.unit


def test_health_check_live_returns_ok() -> None:
    response = asyncio.run(health_check_live())

    assert response.status == "ok"
    assert response.service == "sms-gateway"
    assert response.timestamp <= time.time()


def test_health_routes_are_registered() -> None:
    app = create_app()

    route_paths = set(app.openapi()["paths"])

    assert "/api/v1/health/live" in route_paths
    assert "/api/v1/health/ready" in route_paths

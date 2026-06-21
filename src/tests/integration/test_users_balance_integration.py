from decimal import Decimal

import pytest

from api.routes import users as user_routes
from api.schemas.balance import TopUpUserBalance
from api.schemas.pagination import PaginationParams
from api.schemas.users import CreateUserRequest
from app.usecases.balance import BalanceUseCase
from app.usecases.users import UserUseCase
from domain.errors import UserNotFoundError
from main import create_app
from tests.conftest import SessionFactory

pytestmark = pytest.mark.integration


def test_users_balance_endpoint_flow(db_session_factory: SessionFactory) -> None:
    with db_session_factory() as session:
        created = user_routes.create_user(
            CreateUserRequest(name="Ada"),
            UserUseCase(session),
        )
        user_id = created.id

        assert created.name == "Ada"
        assert created.balance is not None
        assert created.balance.credits == Decimal("0.00")
        assert created.balance.reserved_credits == Decimal("0.00")
        assert created.balance.available_credits == Decimal("0.00")

    with db_session_factory() as session:
        users = user_routes.get_users(
            params=PaginationParams(page=1, size=20),
            usecase=UserUseCase(session),
        )

        assert users.total == 1
        assert users.items[0].id == user_id
        assert users.items[0].name == "Ada"

    with db_session_factory() as session:
        fetched = user_routes.get_user(user_id, UserUseCase(session))

        assert fetched.id == user_id
        assert fetched.balance is not None
        assert fetched.balance.available_credits == Decimal("0.00")

    with db_session_factory() as session:
        topped_up = user_routes.topup_user_balance(
            user_id,
            TopUpUserBalance(credit_amount=Decimal("25.00")),
            BalanceUseCase(session),
        )

        assert topped_up.user_id == user_id
        assert topped_up.credits == Decimal("25.00")
        assert topped_up.available_credits == Decimal("25.00")

    with db_session_factory() as session:
        zeroed = user_routes.zero_user_balance(user_id, BalanceUseCase(session))

        assert zeroed.user_id == user_id
        assert zeroed.credits == Decimal("0.00")
        assert zeroed.reserved_credits == Decimal("0.00")
        assert zeroed.available_credits == Decimal("0.00")


def test_get_user_endpoint_raises_for_missing_user(db_session_factory: SessionFactory) -> None:
    with db_session_factory() as session, pytest.raises(UserNotFoundError) as exc_info:
        user_routes.get_user(999, UserUseCase(session))

    assert exc_info.value.user_id == 999


def test_topup_endpoint_raises_for_missing_user(db_session_factory: SessionFactory) -> None:
    with db_session_factory() as session, pytest.raises(UserNotFoundError) as exc_info:
        user_routes.topup_user_balance(
            999,
            TopUpUserBalance(credit_amount=Decimal("25.00")),
            BalanceUseCase(session),
        )

    assert exc_info.value.user_id == 999


def test_zero_endpoint_raises_for_missing_user(db_session_factory: SessionFactory) -> None:
    with db_session_factory() as session, pytest.raises(UserNotFoundError) as exc_info:
        user_routes.zero_user_balance(999, BalanceUseCase(session))

    assert exc_info.value.user_id == 999


def test_user_routes_are_registered_in_app() -> None:
    app = create_app()
    route_paths = set(app.openapi()["paths"])

    assert "/api/v1/users/" in route_paths
    assert "/api/v1/users/{user_id}" in route_paths
    assert "/api/v1/users/{user_id}/topup" in route_paths
    assert "/api/v1/users/{user_id}/zero" in route_paths

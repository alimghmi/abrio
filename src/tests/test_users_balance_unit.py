import asyncio
from datetime import UTC, datetime
from typing import cast

import pytest
from fastapi.routing import APIRoute
from pydantic import ValidationError

from api.deps import get_balance_usecase, get_user_usecase
from api.routes import users as user_routes
from api.schemas.balance import BalanceIDResponse, TopUpUserBalance
from api.schemas.users import CreateUserRequest, UserResponse
from app.usecases.balance import BalanceUseCase
from app.usecases.users import UserUseCase
from infra.db.models.balance import Balance
from infra.db.models.user import User
from tests.conftest import SessionFactory


class FakeBalance:
    def __init__(
        self,
        *,
        credits: int = 10,
        reserved_credits: int = 3,
        user_id: int = 1,
    ) -> None:
        self.user_id = user_id
        self.credits = credits
        self.reserved_credits = reserved_credits
        self.updated_at = datetime.now(UTC)

    @property
    def available_credits(self) -> int:
        return self.credits - self.reserved_credits


class FakeUser:
    def __init__(self, *, user_id: int = 1, name: str = "Ada") -> None:
        self.id = user_id
        self.name = name
        self.balance = FakeBalance(user_id=user_id)
        self.created_at = datetime.now(UTC)


class FakeUserRepo:
    def __init__(self) -> None:
        self.user = FakeUser()


class FakeUserUseCase:
    def __init__(self) -> None:
        self.repo = FakeUserRepo()

    def create_user(self, payload: CreateUserRequest) -> FakeUser:
        return FakeUser(name=payload.name)

    def get_users(self) -> list[FakeUser]:
        return [self.repo.user]

    def get_user(self, user_id: int) -> FakeUser:
        assert user_id == self.repo.user.id
        return self.repo.user


class FakeBalanceUseCase:
    def topup_user_credits(self, *, user_id: int, credit_amount: int) -> FakeBalance:
        assert user_id == 1
        return FakeBalance(credits=credit_amount, reserved_credits=0, user_id=user_id)

    def zero_user_credits(self, *, user_id: int) -> FakeBalance:
        assert user_id == 1
        return FakeBalance(credits=0, reserved_credits=0, user_id=user_id)


def test_get_users_route_uses_user_usecase() -> None:
    response = asyncio.run(user_routes.get_users(cast(UserUseCase, FakeUserUseCase())))

    assert len(response) == 1
    assert response[0].name == "Ada"


def test_create_user_route_uses_user_usecase() -> None:
    response = asyncio.run(
        user_routes.create_user(
            CreateUserRequest(name="Grace"),
            cast(UserUseCase, FakeUserUseCase()),
        )
    )

    assert response.name == "Grace"


def test_get_user_route_uses_user_usecase() -> None:
    response = asyncio.run(user_routes.get_user(1, cast(UserUseCase, FakeUserUseCase())))

    assert response.id == 1
    assert response.balance.available_credits == 7


def test_topup_route_uses_balance_usecase() -> None:
    response = asyncio.run(
        user_routes.topup_user_balance(
            1,
            TopUpUserBalance(credit_amount=25),
            cast(BalanceUseCase, FakeBalanceUseCase()),
        )
    )

    assert response.credits == 25
    assert response.available_credits == 25


def test_zero_route_uses_balance_usecase() -> None:
    response = asyncio.run(
        user_routes.zero_user_balance(1, cast(BalanceUseCase, FakeBalanceUseCase()))
    )

    assert response.credits == 0
    assert response.reserved_credits == 0


def test_create_user_request_rejects_invalid_names() -> None:
    with pytest.raises(ValidationError):
        CreateUserRequest(name="")

    with pytest.raises(ValidationError):
        CreateUserRequest(name="x" * 256)


def test_topup_user_balance_rejects_non_positive_amounts() -> None:
    with pytest.raises(ValidationError):
        TopUpUserBalance(credit_amount=0)

    with pytest.raises(ValidationError):
        TopUpUserBalance(credit_amount=-1)


def test_user_response_validates_orm_like_object() -> None:
    response = UserResponse.model_validate(FakeUser())

    assert response.name == "Ada"
    assert response.balance is not None
    assert response.balance.available_credits == 7


def test_balance_id_response_validates_orm_like_object() -> None:
    response = BalanceIDResponse.model_validate(FakeBalance())

    assert response.user_id == 1
    assert response.available_credits == 7


def test_user_routes_are_registered() -> None:
    route_paths = {route.path for route in user_routes.router.routes if isinstance(route, APIRoute)}

    assert "/" in route_paths
    assert "/{user_id}" in route_paths
    assert "/{user_id}/topup" in route_paths
    assert "/{user_id}/zero" in route_paths


def test_dependency_factories_create_usecases(db_session_factory: SessionFactory) -> None:
    with db_session_factory() as session:
        user_usecase = get_user_usecase(session)
        balance_usecase = get_balance_usecase(session)

    assert isinstance(user_usecase, UserUseCase)
    assert isinstance(balance_usecase, BalanceUseCase)


def test_balance_available_credits_property() -> None:
    balance = Balance(credits=12, reserved_credits=5)

    assert balance.available_credits == 7


def test_user_model_accepts_optional_balance() -> None:
    user = User(name="Linus")

    assert user.name == "Linus"
    assert user.balance is None

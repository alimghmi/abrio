import pytest
from sqlalchemy import select

from api.schemas.users import CreateUserRequest
from app.usecases.balance import BalanceUseCase
from app.usecases.users import UserUseCase
from domain.errors import UserNotFoundError
from infra.db.models.balance import Balance
from infra.db.models.user import User
from infra.db.repositories.balance import BalanceRepositry
from infra.db.repositories.users import UserRepository
from tests.conftest import SeedUser, SessionFactory

pytestmark = pytest.mark.integration


def test_user_repository_create_user_adds_zero_balance(db_session_factory: SessionFactory) -> None:
    with db_session_factory() as session:
        user = UserRepository(session).create_user("Ada")
        session.commit()
        session.refresh(user)

        assert user.id is not None
        assert user.name == "Ada"
        assert user.balance is not None
        assert user.balance.credits == 0
        assert user.balance.reserved_credits == 0


def test_user_repository_get_users_returns_balances(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    seed_user("Ada", 10, 4)
    seed_user("Grace", 20, 5)

    with db_session_factory() as session:
        users = UserRepository(session).get_users()

        assert [user.name for user in users] == ["Ada", "Grace"]
        assert users[0].balance.available_credits == 6
        assert users[1].balance.available_credits == 15


def test_user_repository_get_user_returns_balance(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    user_id = seed_user("Ada", 10, 4)

    with db_session_factory() as session:
        user = UserRepository(session).get_user(user_id)

        assert user.name == "Ada"
        assert user.balance.available_credits == 6


def test_user_repository_get_user_raises_for_missing_user(
    db_session_factory: SessionFactory,
) -> None:
    with db_session_factory() as session, pytest.raises(UserNotFoundError) as exc_info:
        UserRepository(session).get_user(999)

    assert exc_info.value.user_id == 999


def test_balance_repository_get_by_user_id_returns_balance(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    user_id = seed_user("Ada", 15, 5)

    with db_session_factory() as session:
        balance = BalanceRepositry(session).get_by_user_id(user_id)

        assert balance is not None
        assert balance.user_id == user_id
        assert balance.available_credits == 10


def test_balance_repository_get_by_user_id_raises_for_missing_user(
    db_session_factory: SessionFactory,
) -> None:
    with db_session_factory() as session, pytest.raises(UserNotFoundError) as exc_info:
        BalanceRepositry(session).get_by_user_id(999)

    assert exc_info.value.user_id == 999


def test_user_usecase_create_user_commits_and_refreshes(
    db_session_factory: SessionFactory,
) -> None:
    with db_session_factory() as session:
        user = UserUseCase(session).create_user(CreateUserRequest(name="Ada"))
        user_id = user.id

    with db_session_factory() as session:
        persisted = session.get(User, user_id)

        assert persisted is not None
        assert persisted.name == "Ada"
        assert persisted.balance is not None
        assert persisted.balance.credits == 0


def test_balance_usecase_topup_user_credits(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    user_id = seed_user("Ada", 10, 3)

    with db_session_factory() as session:
        balance = BalanceUseCase(session).topup_user_credits(user_id=user_id, credit_amount=25)

        assert balance.credits == 35
        assert balance.available_credits == 32

    with db_session_factory() as session:
        persisted = session.scalar(select(Balance).where(Balance.user_id == user_id))

        assert persisted is not None
        assert persisted.credits == 35


def test_balance_usecase_zero_user_credits(
    db_session_factory: SessionFactory,
    seed_user: SeedUser,
) -> None:
    user_id = seed_user("Ada", 10, 3)

    with db_session_factory() as session:
        balance = BalanceUseCase(session).zero_user_credits(user_id=user_id)

        assert balance.credits == 0
        assert balance.reserved_credits == 0

    with db_session_factory() as session:
        persisted = session.scalar(select(Balance).where(Balance.user_id == user_id))

        assert persisted is not None
        assert persisted.credits == 0
        assert persisted.reserved_credits == 0

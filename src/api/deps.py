from fastapi import Depends
from sqlalchemy.orm import Session

from app.usecases.balance import BalanceUseCase
from app.usecases.users import UserUseCase
from infra.db.session import get_db


def get_user_usecase(session: Session = Depends(get_db)) -> UserUseCase:
    return UserUseCase(session)


def get_balance_usecase(session: Session = Depends(get_db)) -> BalanceUseCase:
    return BalanceUseCase(session)

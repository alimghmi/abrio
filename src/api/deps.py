from fastapi import Depends
from sqlalchemy.orm import Session

from app.usecases.balance import BalanceUseCase
from app.usecases.messages import MessageUseCase
from app.usecases.users import UserUseCase
from core.config import Settings, get_settings
from infra.db.session import get_db


def get_user_usecase(session: Session = Depends(get_db)) -> UserUseCase:
    return UserUseCase(session)


def get_balance_usecase(session: Session = Depends(get_db)) -> BalanceUseCase:
    return BalanceUseCase(session)


def get_message_usecase(
    session: Session = Depends(get_db), settings: Settings = Depends(get_settings)
) -> MessageUseCase:
    return MessageUseCase(session, settings)

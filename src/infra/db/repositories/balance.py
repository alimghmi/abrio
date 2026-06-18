from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.errors import UserNotFoundError
from infra.db.models.balance import Balance


class BalanceRepositry:
    def __init__(self, session: Session):
        self.session = session

    def get_by_user_id(self, user_id: int) -> Balance | None:
        query = select(Balance).where(Balance.user_id == user_id).with_for_update()
        balance = self.session.scalar(query)

        if balance is None:
            raise UserNotFoundError(user_id)

        return balance

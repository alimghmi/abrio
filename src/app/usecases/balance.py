from sqlalchemy.orm import Session

from infra.db.models.balance import Balance
from infra.db.repositories.balance import BalanceRepositry


class BalanceUseCase:
    def __init__(self, session: Session):
        self.session = session
        self._repo = BalanceRepositry(session)

    def topup_user_credits(self, user_id: int, credit_amount: int) -> Balance:
        with self.session.begin():
            balance = self._repo._topup_user_credits(user_id=user_id, amount=credit_amount)
            return balance

    def zero_user_credits(self, user_id: int) -> Balance:
        with self.session.begin():
            balance = self._repo._zero_user_credits(user_id=user_id)
            return balance

from sqlalchemy.orm import Session

from infra.db.models.balance import Balance
from infra.db.repositories.balance import BalanceRepositry


class BalanceUseCase:
    def __init__(self, session: Session):
        self.session = session
        self._repo = BalanceRepositry(session)

    def topup_user_credits(self, user_id: int, credit_amount: int) -> Balance:
        with self.session.begin():
            balance = self._repo.get_by_user_id(user_id=user_id)
            if not balance:
                raise ValueError("User balance record not found")

            balance.credits += credit_amount
            return balance

    def zero_user_credits(self, user_id: int) -> Balance:
        with self.session.begin():
            balance = self._repo.get_by_user_id(user_id=user_id)
            if not balance:
                raise ValueError("User balance record not found")

            balance.credits = 0
            balance.reserved_credits = 0
            return balance

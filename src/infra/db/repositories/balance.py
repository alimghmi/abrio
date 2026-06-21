from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.errors import InvalidAmountValueError, UserNotFoundError
from infra.db.models.balance import Balance


class BalanceRepositry:
    def __init__(self, session: Session):
        self.session = session

    def get_by_user_id(self, user_id: int) -> Balance:
        query = select(Balance).where(Balance.user_id == user_id).with_for_update()
        balance = self.session.scalar(query)

        if balance is None:
            raise UserNotFoundError(user_id)

        return balance

    def _topup_user_credits(self, user_id: int, amount: Decimal) -> Balance:
        self._validate_amount(amount)
        balance = self.get_by_user_id(user_id=user_id)
        balance.credits += amount
        return balance

    def _deduct_user_credits(self, user_id: int, amount: Decimal) -> Balance:
        self._validate_amount(amount)
        balance = self.get_by_user_id(user_id=user_id)
        balance.credits -= amount
        return balance

    def _topup_user_reserved_credits(self, user_id: int, amount: Decimal) -> Balance:
        self._validate_amount(amount)
        balance = self.get_by_user_id(user_id=user_id)
        balance.reserved_credits += amount
        return balance

    def _deduct_user_reserved_credits(self, user_id: int, amount: Decimal) -> Balance:
        self._validate_amount(amount)
        balance = self.get_by_user_id(user_id=user_id)
        balance.reserved_credits -= amount
        return balance

    def _zero_user_credits(self, user_id: int) -> Balance:
        balance = self.get_by_user_id(user_id=user_id)
        balance.credits = Decimal("0.00")
        balance.reserved_credits = Decimal("0.00")
        return balance

    def reserve_credits(self, user_id: int, amount: Decimal) -> Balance:
        self._validate_amount(amount)
        return self._topup_user_reserved_credits(user_id=user_id, amount=amount)

    def release_credits(self, user_id: int, amount: Decimal) -> Balance:
        self._validate_amount(amount)
        return self._deduct_user_reserved_credits(user_id=user_id, amount=amount)

    def settle(self, user_id: int, amount: Decimal) -> Balance:
        self._validate_amount(amount)
        balance = self.get_by_user_id(user_id=user_id)
        balance.reserved_credits -= amount
        balance.credits -= amount
        return balance

    @staticmethod
    def _validate_amount(amount: Decimal):
        if amount <= Decimal("0"):
            raise InvalidAmountValueError(amount)

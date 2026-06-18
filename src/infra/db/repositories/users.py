from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from domain.errors import UserNotFoundError
from infra.db.models.balance import Balance
from infra.db.models.user import User


class UserRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_users(self) -> list[User]:
        query = select(User).options(joinedload(User.balance))
        return list(self.session.scalars(query).all())

    def get_user(self, user_id: int) -> User:
        query = select(User).options(joinedload(User.balance)).where(User.id == user_id)
        user = self.session.scalar(query)
        if user is None:
            raise UserNotFoundError(user_id)

        return user

    def create_user(self, name) -> User:
        new_user = User(name=name, balance=Balance(credits=0, reserved_credits=0))
        self.session.add(new_user)
        return new_user

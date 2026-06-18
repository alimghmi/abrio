from sqlalchemy.orm import Session

from api.schemas.users import CreateUserRequest
from infra.db.models import User
from infra.db.repositories.users import UserRepository


class UserUseCase:
    def __init__(self, session: Session):
        self.session = session
        self.repo = UserRepository(session)

    def create_user(self, payload: CreateUserRequest) -> User:
        with self.session.begin():
            new_user = self.repo.create_user(payload.name)

        self.session.refresh(new_user)
        return new_user

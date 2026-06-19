from sqlalchemy.orm import Session

from api.schemas.pagination import PaginatedResponse, PaginationParams  # type: ignore
from api.schemas.users import CreateUserRequest
from infra.db.models import User
from infra.db.repositories.users import UserRepository


class UserUseCase:
    def __init__(self, session: Session):
        self.session = session
        self._repo = UserRepository(session)

    def get_users(self) -> list[User]:
        return self._repo.get_users()

    def get_users_slice(self, params: PaginationParams) -> PaginatedResponse:
        users, total = self._repo.get_users_slice(limit=params.limit, offset=params.offset)
        return PaginatedResponse.make(items=users, total=total, params=params)

    def get_user(self, user_id: int) -> User:
        return self._repo.get_user(user_id=user_id)

    def create_user(self, payload: CreateUserRequest) -> User:
        with self.session.begin():
            new_user = self._repo.create_user(payload.name)

        self.session.refresh(new_user)
        return new_user

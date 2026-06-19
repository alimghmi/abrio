from uuid import UUID

from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from api.schemas.messages import MessageRequest
from core.config import Settings
from domain.errors import InsufficientBalanceError, UserNotFoundError
from infra.db.models.message import Message
from infra.db.repositories.balance import BalanceRepositry
from infra.db.repositories.messages import MessageRepository


class MessageUseCase:
    def __init__(self, session: Session, settings: Settings):
        self.session = session
        self.settings = settings
        self._repo = MessageRepository(session)
        self._balance_repo = BalanceRepositry(session)

    def get_user_messages(self, user_id: int) -> list[Message]:
        return self._repo.get_user_messages(user_id=user_id)

    def get_user_message(self, message_id: UUID, user_id: int) -> Message:
        return self._repo.get_user_message(message_id=message_id, user_id=user_id)

    def create_message(self, payload: MessageRequest) -> Message:
        message_cost = self.settings.cost_per_message
        try:
            with self.session.begin():
                payload_dict = {**payload.model_dump(), "cost": message_cost}
                message = self._repo.create_message(payload=payload_dict)
                self.session.flush()
                self._balance_repo.reserve_credits(payload.user_id, message_cost)
        except IntegrityError as e:
            if isinstance(e.orig, UniqueViolation):
                return self._repo.get_user_message_by_idempotency_key(
                    payload.user_id, payload.idempotency_key
                )

            elif isinstance(e.orig, ForeignKeyViolation):
                raise UserNotFoundError(user_id=payload.user_id) from e

            elif isinstance(e.orig, CheckViolation) and "balances" in e.__str__():
                raise InsufficientBalanceError(payload.user_id, message_cost=message_cost) from e

            raise e

        return message

    def calculate_summary(self, user_id: int) -> dict[str, int]:
        return self._repo.calculate_summary(user_id=user_id)

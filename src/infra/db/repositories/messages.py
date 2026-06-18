from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from domain.errors import MessageNotFoundError
from infra.db.models.message import Message


class MessageRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_user_messages(self, user_id: int) -> list[Message]:
        query = select(Message).where(Message.user_id == user_id)
        return list(self.session.scalars(query).all())

    def get_user_message_by_idempotency_key(self, user_id: int, idempotency_key: UUID) -> Message:
        query = select(Message).where(
            Message.idempotency_key == idempotency_key, Message.user_id == user_id
        )
        message = self.session.scalar(query)
        if message is None:
            raise MessageNotFoundError(idempotency_key=idempotency_key)

        return message

    def get_user_message(self, message_id: UUID, user_id: int) -> Message:
        query = select(Message).where(Message.id == message_id, Message.user_id == user_id)
        message = self.session.scalar(query)
        if message is None:
            raise MessageNotFoundError(message_id=message_id)

        return message

    def create_message(self, payload: dict) -> Message:
        new_message = Message(**payload)
        self.session.add(new_message)
        return new_message

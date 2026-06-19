from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from domain.enums import MessageStatus, PaymentStatus
from domain.errors import MessageNotFoundError
from infra.db.models.message import Message


class MessageRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_user_messages(self, user_id: int) -> list[Message]:
        query = select(Message).where(Message.user_id == user_id)
        return list(self.session.scalars(query).all())

    def get_user_messages_slice(
        self, limit: int, offset: int, **kwargs
    ) -> tuple[list[Message], int]:
        user_filter = Message.user_id == kwargs.get("user_id")
        count_query = select(func.count(Message.id)).where(user_filter)
        total_count = self.session.scalar(count_query) or 0
        data_query = select(Message).where(user_filter).limit(limit).offset(offset)
        messages = list(self.session.scalars(data_query).all())
        return messages, total_count

    def get_user_message_by_idempotency_key(self, user_id: int, idempotency_key: UUID) -> Message:
        query = select(Message).where(
            Message.idempotency_key == idempotency_key, Message.user_id == user_id
        )
        message = self.session.scalar(query)
        if message is None:
            raise MessageNotFoundError(idempotency_key=idempotency_key)

        return message

    def _get_message(self, message_id: UUID) -> Message:
        query = select(Message).where(Message.id == message_id)
        message = self.session.scalar(query)
        if message is None:
            raise MessageNotFoundError(message_id=message_id)

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

    def update_message_status(self, message_id: UUID, status: MessageStatus):
        query = (
            update(Message).where(Message.id == message_id).values(status=status).returning(Message)
        )
        message = self.session.scalar(query)
        if message is None:
            raise MessageNotFoundError(message_id=message_id)

        return message

    def update_message_payment_status(self, message_id: UUID, payment_status: PaymentStatus):
        query = (
            update(Message)
            .where(Message.id == message_id)
            .values(payment_status=payment_status)
            .returning(Message)
        )
        message = self.session.scalar(query)
        if message is None:
            raise MessageNotFoundError(message_id=message_id)

        return message

    def calculate_summary(self, user_id: int) -> dict[str, int]:
        query = select(
            func.count(Message.id).label("total"),
            func.count(Message.id).filter(Message.status == MessageStatus.QUEUED).label("queued"),
            func.count(Message.id)
            .filter(Message.status == MessageStatus.DISPATCHING)
            .label("dispatching"),
            func.count(Message.id).filter(Message.status == MessageStatus.FAILED).label("failed"),
            func.count(Message.id).filter(Message.status == MessageStatus.SENT).label("sent"),
            func.count(Message.id)
            .filter(Message.status == MessageStatus.PERMANENT_FAILED)
            .label("permanent_failed"),
        ).where(Message.user_id == user_id)
        result = self.session.execute(query).one()
        return {
            "user_id": user_id,
            "total": result.total,
            "queued": result.queued,
            "dispatching": result.dispatching,
            "failed": result.failed,
            "sent": result.sent,
            "permanent_failed": result.permanent_failed,
        }

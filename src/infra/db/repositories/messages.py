from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from domain.enums import MessagePriority, MessageStatus, PaymentStatus
from domain.errors import MessageNotFoundError
from infra.db.models.message import Message


class MessageRepository:
    def __init__(self, session: Session):
        self.session = session

    def get_user_messages(self, user_id: int) -> list[Message]:
        query = select(Message).where(Message.user_id == user_id)
        return list(self.session.scalars(query).all())

    def get_messages_slice(
        self,
        limit: int,
        offset: int,
        user_id: int | None = None,
        status: MessageStatus | None = None,
        priority: MessagePriority | None = None,
        payment_status: PaymentStatus | None = None,
        created_after: datetime | None = None,
        created_before: datetime | None = None,
        updated_after: datetime | None = None,
        updated_before: datetime | None = None,
    ) -> tuple[list[Message], int]:
        data_query = select(Message)
        count_query = select(func.count(Message.id))

        if user_id:
            data_query = select(Message).where(Message.user_id == user_id)
            count_query = select(func.count(Message.id)).where(Message.user_id == user_id)
        if status:
            data_query = data_query.where(Message.status == status)
            count_query = count_query.where(Message.status == status)
        if priority:
            data_query = data_query.where(Message.priority == priority)
            count_query = count_query.where(Message.priority == priority)
        if payment_status:
            data_query = data_query.where(Message.payment_status == payment_status)
            count_query = count_query.where(Message.payment_status == payment_status)
        if created_after:
            data_query = data_query.where(Message.created_at >= created_after)
            count_query = count_query.where(Message.created_at >= created_after)
        if created_before:
            data_query = data_query.where(Message.created_at <= created_before)
            count_query = count_query.where(Message.created_at <= created_before)
        if updated_after:
            data_query = data_query.where(Message.updated_at >= updated_after)
            count_query = count_query.where(Message.updated_at >= updated_after)
        if updated_before:
            data_query = data_query.where(Message.updated_at <= updated_before)
            count_query = count_query.where(Message.updated_at <= updated_before)

        data_query = data_query.limit(limit).offset(offset).order_by(Message.created_at.desc())
        total_count = self.session.scalar(count_query) or 0
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

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, status

from api.deps import get_message_usecase
from api.schemas.messages import MessageRequest, MessageResponse, MessagesSummaryResponse
from api.schemas.pagination import PaginatedResponse, PaginationParams
from app.usecases.messages import MessageUseCase
from domain.enums import MessagePriority, MessageStatus, PaymentStatus

router = APIRouter()


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=MessageResponse)
async def send_message(
    payload: MessageRequest, usecase: MessageUseCase = Depends(get_message_usecase)
):
    return usecase.create_message(payload=payload)


@router.get("/", status_code=status.HTTP_200_OK, response_model=PaginatedResponse[MessageResponse])
async def get_messages(
    user_id: int | None = None,
    status: MessageStatus | None = None,
    priority: MessagePriority | None = None,
    payment_status: PaymentStatus | None = None,
    created_after: datetime | None = None,
    created_before: datetime | None = None,
    updated_after: datetime | None = None,
    updated_before: datetime | None = None,
    params: PaginationParams = Depends(),
    usecase: MessageUseCase = Depends(get_message_usecase),
):
    return usecase.get_messages_slice(
        user_id=user_id,
        status=status,
        priority=priority,
        payment_status=payment_status,
        created_after=created_after,
        created_before=created_before,
        updated_after=updated_after,
        updated_before=updated_before,
        params=params,
    )


@router.get("/summary", status_code=status.HTTP_200_OK, response_model=MessagesSummaryResponse)
async def get_user_messages_summary(
    user_id: int, usecase: MessageUseCase = Depends(get_message_usecase)
):
    return usecase.calculate_summary(user_id=user_id)


@router.get("/{message_id}", status_code=status.HTTP_200_OK, response_model=MessageResponse)
async def get_message_by_id(
    message_id: UUID, user_id: int, usecase: MessageUseCase = Depends(get_message_usecase)
):
    return usecase.get_user_message(message_id=message_id, user_id=user_id)

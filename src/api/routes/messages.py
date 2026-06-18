from uuid import UUID

from fastapi import APIRouter, Depends, status

from api.deps import get_message_usecase
from api.schemas.messages import MessageRequest, MessageResponse
from app.usecases.messages import MessageUseCase

router = APIRouter()


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=MessageResponse)
async def send_message(
    payload: MessageRequest, usecase: MessageUseCase = Depends(get_message_usecase)
):
    return usecase.create_message(payload=payload)


@router.get("/", status_code=status.HTTP_200_OK, response_model=list[MessageResponse])
async def get_user_messages(user_id: int, usecase: MessageUseCase = Depends(get_message_usecase)):
    return usecase.get_user_messages(user_id=user_id)


@router.get("/{message_id}", status_code=status.HTTP_200_OK, response_model=MessageResponse)
async def get_message_by_id(
    message_id: UUID, user_id: int, usecase: MessageUseCase = Depends(get_message_usecase)
):
    return usecase.get_user_message(message_id=message_id, user_id=user_id)


@router.get("/summary", status_code=status.HTTP_200_OK)
async def get_user_messages_summary(user_id: int):
    # WIP
    return {"status": user_id}

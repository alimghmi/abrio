from fastapi import APIRouter, Depends, status

from api.deps import get_balance_usecase, get_user_usecase
from api.schemas.balance import BalanceIDResponse, TopUpUserBalance
from api.schemas.users import CreateUserRequest, UserResponse
from app.usecases.balance import BalanceUseCase
from app.usecases.users import UserUseCase

router = APIRouter()


@router.get("/", status_code=status.HTTP_200_OK, response_model=list[UserResponse])
async def get_users(usecase: UserUseCase = Depends(get_user_usecase)):
    return usecase.get_users()


@router.post("/", status_code=status.HTTP_201_CREATED, response_model=UserResponse)
async def create_user(payload: CreateUserRequest, usecase: UserUseCase = Depends(get_user_usecase)):
    return usecase.create_user(payload)


@router.get("/{user_id}", status_code=status.HTTP_200_OK, response_model=UserResponse)
async def get_user(user_id: int, usecase: UserUseCase = Depends(get_user_usecase)):
    return usecase.get_user(user_id=user_id)


@router.post("/{user_id}/topup", status_code=status.HTTP_200_OK, response_model=BalanceIDResponse)
async def topup_user_balance(
    user_id: int,
    payload: TopUpUserBalance,
    usecase: BalanceUseCase = Depends(get_balance_usecase),
):
    return usecase.topup_user_credits(user_id=user_id, credit_amount=payload.credit_amount)


@router.post("/{user_id}/zero", status_code=status.HTTP_200_OK, response_model=BalanceIDResponse)
async def zero_user_balance(
    user_id: int,
    usecase: BalanceUseCase = Depends(get_balance_usecase),
):
    return usecase.zero_user_credits(user_id=user_id)

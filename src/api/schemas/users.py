from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class BalanceResponse(BaseModel):
    credits: int
    reserved_credits: int
    available_credits: int
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class BalanceIDResponse(BalanceResponse):
    user_id: int


class UserResponse(BaseModel):
    id: int
    name: str
    balance: BalanceResponse | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CreateUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class TopUpUserBalance(BaseModel):
    credit_amount: int = Field(gt=0)

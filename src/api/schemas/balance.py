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


class TopUpUserBalance(BaseModel):
    credit_amount: int = Field(gt=0)

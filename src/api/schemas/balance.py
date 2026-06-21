from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_serializer


class BalanceResponse(BaseModel):
    credits: Decimal
    reserved_credits: Decimal
    available_credits: Decimal
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("credits", "reserved_credits", "available_credits")
    def serialize_decimal(self, value: Decimal) -> str:
        return f"{value:.2f}"


class BalanceIDResponse(BalanceResponse):
    user_id: int


class TopUpUserBalance(BaseModel):
    credit_amount: Decimal = Field(gt=0)

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from api.schemas.balance import BalanceResponse


class UserResponse(BaseModel):
    id: int
    name: str
    balance: BalanceResponse | None = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class CreateUserRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)

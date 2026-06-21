from decimal import Decimal

from pydantic import BaseModel, Field

from core.config import get_settings

settings = get_settings()


class PricingResponse(BaseModel):
    normal_message: Decimal = Field(default=settings.cost_per_message)
    express_message: Decimal = Field(default=settings.cost_per_express_message)

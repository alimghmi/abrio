from fastapi import APIRouter

from api.schemas.pricing import PricingResponse

router = APIRouter()


@router.get("/")
def get_pricing():
    return PricingResponse()

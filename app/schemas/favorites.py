from datetime import datetime

from pydantic import BaseModel

from app.schemas.points import LocationOut, OilType


class FavoritePointOut(BaseModel):
    """Mesmo formato do PointSummary, sem distance_m, com favorited_at."""

    id: str
    name: str
    bairro: str
    address_formatted: str
    location: LocationOut
    accepted_oils: list[OilType]
    verified: bool
    favorited_at: datetime


class FavoritesListOut(BaseModel):
    items: list[FavoritePointOut]

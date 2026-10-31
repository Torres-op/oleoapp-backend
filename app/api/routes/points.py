from typing import Annotated

from fastapi import APIRouter, Query

from app.api.deps import CurrentUserDep, Db
from app.schemas.points import PointOut, PointsNearOut
from app.services import points as points_service

router = APIRouter(prefix="/points", tags=["points"])


@router.get("/near", response_model=PointsNearOut)
async def near_points(
    db: Db,
    _user: CurrentUserDep,
    lat: Annotated[float, Query(ge=-90, le=90)],
    lng: Annotated[float, Query(ge=-180, le=180)],
    radius_m: Annotated[int, Query(ge=100, le=50_000)] = 10_000,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> PointsNearOut:
    items = await points_service.find_near(db, lat, lng, radius_m, limit)
    return PointsNearOut(items=items)


@router.get("/{point_id}", response_model=PointOut)
async def get_point(point_id: str, db: Db, _user: CurrentUserDep) -> PointOut:
    return await points_service.get_point(db, point_id)

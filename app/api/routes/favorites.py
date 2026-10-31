from fastapi import APIRouter, Response

from app.api.deps import CurrentUserDep, Db
from app.schemas.favorites import FavoritesListOut
from app.services import favorites as favorites_service

router = APIRouter(prefix="/favorites", tags=["favorites"])


@router.get("", response_model=FavoritesListOut)
async def list_favorites(db: Db, user: CurrentUserDep) -> FavoritesListOut:
    items = await favorites_service.list_favorites(db, user)
    return FavoritesListOut(items=items)


@router.put("/{point_id}", status_code=204)
async def add_favorite(point_id: str, db: Db, user: CurrentUserDep) -> Response:
    await favorites_service.add_favorite(db, user, point_id)
    return Response(status_code=204)


@router.delete("/{point_id}", status_code=204)
async def remove_favorite(point_id: str, db: Db, user: CurrentUserDep) -> Response:
    await favorites_service.remove_favorite(db, user, point_id)
    return Response(status_code=204)

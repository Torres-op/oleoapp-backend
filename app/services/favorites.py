from datetime import UTC, datetime

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.api.deps import CurrentUser
from app.repositories import favorites as favorites_repo
from app.schemas.favorites import FavoritePointOut
from app.schemas.points import LocationOut
from app.services.points import address_out, get_active_point


async def list_favorites(db: AsyncDatabase, user: CurrentUser) -> list[FavoritePointOut]:
    favorites = await favorites_repo.list_by_user(db, ObjectId(user.id))
    points = await favorites_repo.find_active_points(db, [fav["point_id"] for fav in favorites])
    points_by_id = {point["_id"]: point for point in points}

    items: list[FavoritePointOut] = []
    for favorite in favorites:  # já vem do mais recente para o mais antigo
        point = points_by_id.get(favorite["point_id"])
        if point is None:  # ponto desativado ou removido: não aparece na lista
            continue
        address = address_out(point.get("address"))
        lng, lat = point["location"]["coordinates"]  # GeoJSON guarda [lng, lat]
        items.append(
            FavoritePointOut(
                id=str(point["_id"]),
                name=point["name"],
                bairro=address.bairro,
                address_formatted=address.formatted,
                location=LocationOut(lat=lat, lng=lng),
                accepted_oils=point["accepted_oils"],
                verified=point["verified"],
                favorited_at=favorite["created_at"],
            )
        )
    return items


async def add_favorite(db: AsyncDatabase, user: CurrentUser, point_id: str) -> None:
    point = await get_active_point(db, point_id)
    await favorites_repo.upsert(db, ObjectId(user.id), point["_id"], datetime.now(UTC))


async def remove_favorite(db: AsyncDatabase, user: CurrentUser, point_id: str) -> None:
    await favorites_repo.delete(db, ObjectId(user.id), point_id)

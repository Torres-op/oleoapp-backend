from datetime import datetime
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from app.db.collections import DISPOSALS

Document = dict[str, Any]


async def create(
    db: AsyncDatabase,
    user_id: ObjectId,
    point_id: ObjectId | None,
    point_name: str | None,
    volume_ml: int,
    created_at: datetime,
) -> Document:
    document: Document = {
        "user_id": user_id,
        "point_id": point_id,
        "point_name": point_name,
        "volume_ml": volume_ml,
        "created_at": created_at,
    }
    result = await db[DISPOSALS].insert_one(document)
    document["_id"] = result.inserted_id
    return document


async def list_by_user(db: AsyncDatabase, user_id: ObjectId, limit: int) -> list[Document]:
    cursor = db[DISPOSALS].find({"user_id": user_id}).sort("created_at", DESCENDING).limit(limit)
    return await cursor.to_list(length=limit)


async def delete_by_user(db: AsyncDatabase, user_id: ObjectId, disposal_id: str) -> bool:
    """Apaga só se o registro for do usuário. Devolve False se não achou."""
    try:
        object_id = ObjectId(disposal_id)
    except (InvalidId, TypeError):
        return False
    result = await db[DISPOSALS].delete_one({"_id": object_id, "user_id": user_id})
    return result.deleted_count == 1


async def totals_by_user(
    db: AsyncDatabase, user_id: ObjectId, since: datetime | None
) -> tuple[int, int]:
    """Soma de volume_ml e quantidade de descartes do usuário (a partir de `since`, se houver)."""
    match: Document = {"user_id": user_id}
    if since is not None:
        match["created_at"] = {"$gte": since}
    pipeline = [
        {"$match": match},
        {"$group": {"_id": None, "total_ml": {"$sum": "$volume_ml"}, "count": {"$sum": 1}}},
    ]
    cursor = await db[DISPOSALS].aggregate(pipeline)
    rows = await cursor.to_list(length=1)
    if not rows:
        return 0, 0
    return int(rows[0]["total_ml"]), int(rows[0]["count"])


async def find_latest_by_user(db: AsyncDatabase, user_id: ObjectId) -> Document | None:
    return await db[DISPOSALS].find_one({"user_id": user_id}, sort=[("created_at", DESCENDING)])

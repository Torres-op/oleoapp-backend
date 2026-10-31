from datetime import datetime
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import DESCENDING
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.db.collections import FAVORITES, POINTS

Document = dict[str, Any]


async def upsert(
    db: AsyncDatabase, user_id: ObjectId, point_id: ObjectId, created_at: datetime
) -> None:
    """Idempotente: se o favorito já existe, mantém o created_at original."""
    try:
        await db[FAVORITES].update_one(
            {"user_id": user_id, "point_id": point_id},
            {"$setOnInsert": {"created_at": created_at}},
            upsert=True,
        )
    except DuplicateKeyError:
        # Duas requisições simultâneas inseriram ao mesmo tempo: o favorito já existe.
        pass


async def delete(db: AsyncDatabase, user_id: ObjectId, point_id: str) -> None:
    """Idempotente: id inválido ou favorito inexistente não geram erro."""
    try:
        object_id = ObjectId(point_id)
    except (InvalidId, TypeError):
        return
    await db[FAVORITES].delete_one({"user_id": user_id, "point_id": object_id})


async def list_by_user(db: AsyncDatabase, user_id: ObjectId) -> list[Document]:
    cursor = db[FAVORITES].find({"user_id": user_id}).sort("created_at", DESCENDING)
    return await cursor.to_list(length=None)


async def find_active_points(db: AsyncDatabase, point_ids: list[ObjectId]) -> list[Document]:
    if not point_ids:
        return []
    cursor = db[POINTS].find({"_id": {"$in": point_ids}, "active": True})
    return await cursor.to_list(length=None)

from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.db.collections import SESSIONS


async def create(
    db: AsyncDatabase, user_id: ObjectId, token_hash: str, expires_at: datetime
) -> None:
    await db[SESSIONS].insert_one(
        {
            "token_hash": token_hash,
            "user_id": user_id,
            "created_at": datetime.now(UTC),
            "expires_at": expires_at,
        }
    )


async def find_valid(db: AsyncDatabase, token_hash: str) -> dict[str, Any] | None:
    # O TTL do Mongo apaga com atraso; por isso conferimos expires_at na leitura.
    return await db[SESSIONS].find_one(
        {"token_hash": token_hash, "expires_at": {"$gt": datetime.now(UTC)}}
    )


async def delete_by_hash(db: AsyncDatabase, token_hash: str) -> None:
    await db[SESSIONS].delete_one({"token_hash": token_hash})


async def delete_by_user(
    db: AsyncDatabase, user_id: ObjectId, except_hash: str | None = None
) -> int:
    query: dict[str, Any] = {"user_id": user_id}
    if except_hash is not None:
        query["token_hash"] = {"$ne": except_hash}
    result = await db[SESSIONS].delete_many(query)
    return result.deleted_count

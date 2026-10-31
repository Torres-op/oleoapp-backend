from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.db.collections import PASSWORD_RESETS

Document = dict[str, Any]


async def create(
    db: AsyncDatabase, user_id: ObjectId, token_hash: str, expires_at: datetime
) -> None:
    await db[PASSWORD_RESETS].insert_one(
        {
            "user_id": user_id,
            "token_hash": token_hash,
            "created_at": datetime.now(UTC),
            "expires_at": expires_at,
            "used_at": None,
        }
    )


async def delete_unused_by_user(db: AsyncDatabase, user_id: ObjectId) -> int:
    result = await db[PASSWORD_RESETS].delete_many({"user_id": user_id, "used_at": None})
    return result.deleted_count


async def find_valid(db: AsyncDatabase, token_hash: str) -> Document | None:
    # O TTL do Mongo apaga com atraso; por isso conferimos expires_at na leitura.
    return await db[PASSWORD_RESETS].find_one(
        {"token_hash": token_hash, "used_at": None, "expires_at": {"$gt": datetime.now(UTC)}}
    )


async def mark_used(db: AsyncDatabase, reset_id: ObjectId) -> bool:
    """Marca como usado. Devolve False se outra requisição já usou (uso único, sem corrida)."""
    result = await db[PASSWORD_RESETS].update_one(
        {"_id": reset_id, "used_at": None}, {"$set": {"used_at": datetime.now(UTC)}}
    )
    return result.modified_count == 1

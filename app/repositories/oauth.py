from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.db.collections import OAUTH_PENDING_LINKS, OAUTH_STATES

Document = dict[str, Any]


async def create_state(db: AsyncDatabase, document: Document) -> None:
    await db[OAUTH_STATES].insert_one(document)


async def pop_state(db: AsyncDatabase, state_hash: str) -> Document | None:
    """Lê e apaga o estado (uso único). O TTL do Mongo apaga com atraso: confere expires_at."""
    return await db[OAUTH_STATES].find_one_and_delete(
        {"state_hash": state_hash, "expires_at": {"$gt": datetime.now(UTC)}}
    )


async def create_pending_link(db: AsyncDatabase, document: Document) -> None:
    await db[OAUTH_PENDING_LINKS].insert_one(document)


async def find_pending_link(db: AsyncDatabase, token_hash: str) -> Document | None:
    return await db[OAUTH_PENDING_LINKS].find_one(
        {"token_hash": token_hash, "expires_at": {"$gt": datetime.now(UTC)}}
    )


async def delete_pending_link(db: AsyncDatabase, token_hash: str) -> None:
    await db[OAUTH_PENDING_LINKS].delete_one({"token_hash": token_hash})


async def delete_pending_links_by_user(db: AsyncDatabase, user_id: ObjectId) -> None:
    await db[OAUTH_PENDING_LINKS].delete_many({"user_id": user_id})

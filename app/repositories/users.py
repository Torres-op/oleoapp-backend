from typing import Any

from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.db.collections import (
    DISPOSALS,
    FAVORITES,
    OAUTH_PENDING_LINKS,
    PASSWORD_RESETS,
    USERS,
)

Document = dict[str, Any]


async def find_by_email(db: AsyncDatabase, email: str) -> Document | None:
    return await db[USERS].find_one({"email": email})


async def find_by_oauth(db: AsyncDatabase, provider: str, provider_user_id: str) -> Document | None:
    return await db[USERS].find_one({f"oauth.{provider}": provider_user_id})


async def find_by_id(db: AsyncDatabase, user_id: ObjectId) -> Document | None:
    return await db[USERS].find_one({"_id": user_id})


async def insert(db: AsyncDatabase, document: Document) -> ObjectId:
    result = await db[USERS].insert_one(document)
    return result.inserted_id


async def update_fields(db: AsyncDatabase, user_id: ObjectId, fields: Document) -> Document | None:
    """Atualiza os campos e devolve o documento já atualizado."""
    return await db[USERS].find_one_and_update(
        {"_id": user_id},
        {"$set": fields},
        return_document=ReturnDocument.AFTER,
    )


async def delete(db: AsyncDatabase, user_id: ObjectId) -> None:
    await db[USERS].delete_one({"_id": user_id})


async def delete_related(db: AsyncDatabase, user_id: ObjectId) -> None:
    """Apaga os dados do usuário em password_resets, disposals, favorites e vínculos pendentes."""
    for collection in (PASSWORD_RESETS, DISPOSALS, FAVORITES, OAUTH_PENDING_LINKS):
        await db[collection].delete_many({"user_id": user_id})

from typing import Any

from bson import ObjectId
from pymongo import DESCENDING
from pymongo.asynchronous.database import AsyncDatabase

from app.db.collections import AUDIT_LOGS

Document = dict[str, Any]


async def insert(db: AsyncDatabase, document: Document) -> None:
    await db[AUDIT_LOGS].insert_one(document)


async def anonymize_user(db: AsyncDatabase, user_id: ObjectId) -> int:
    """Tira os dados pessoais dos eventos do usuário, mantendo os eventos (LGPD)."""
    result = await db[AUDIT_LOGS].update_many(
        {"user_id": user_id},
        {"$set": {"user_id": None, "email": None, "anonymized": True}},
    )
    return result.modified_count


async def count(db: AsyncDatabase, query: Document) -> int:
    return await db[AUDIT_LOGS].count_documents(query)


async def list_page(db: AsyncDatabase, query: Document, skip: int, limit: int) -> list[Document]:
    cursor = db[AUDIT_LOGS].find(query).sort("created_at", DESCENDING).skip(skip).limit(limit)
    return await cursor.to_list(length=limit)


async def aggregate(db: AsyncDatabase, pipeline: list[Document]) -> list[Document]:
    cursor = await db[AUDIT_LOGS].aggregate(pipeline)
    return await cursor.to_list(length=None)

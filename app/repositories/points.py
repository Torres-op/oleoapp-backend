from datetime import UTC, datetime
from typing import Any

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import ASCENDING, ReturnDocument
from pymongo.asynchronous.database import AsyncDatabase

from app.db.collections import POINTS
from app.schemas.points import PointSeed

Document = dict[str, Any]

# Pontos criados ou editados no painel de admin: o import do seed não mexe neles.
MANAGED_BY_ADMIN = "admin"
NOT_MANAGED = {"managed_by": {"$ne": MANAGED_BY_ADMIN}}


def seed_to_document(seed: PointSeed) -> Document:
    """Converte o item do seed no documento do Mongo (GeoJSON guarda [lng, lat])."""
    document = seed.model_dump(mode="python", exclude={"location", "verified_at"})
    document["accepted_oils"] = [oil.value for oil in seed.accepted_oils]
    document["location"] = {"type": "Point", "coordinates": [seed.location.lng, seed.location.lat]}
    document["verified_at"] = (
        datetime(seed.verified_at.year, seed.verified_at.month, seed.verified_at.day, tzinfo=UTC)
        if seed.verified_at
        else None
    )
    return document


async def near(
    db: AsyncDatabase, lat: float, lng: float, radius_m: int, limit: int
) -> list[Document]:
    pipeline = [
        {
            "$geoNear": {
                "near": {"type": "Point", "coordinates": [lng, lat]},
                "distanceField": "distance_m",
                "maxDistance": radius_m,
                "query": {"active": True},
                "spherical": True,
            }
        },
        {"$limit": limit},
    ]
    cursor = await db[POINTS].aggregate(pipeline)
    return await cursor.to_list(length=limit)


async def get_by_id(db: AsyncDatabase, point_id: str) -> Document | None:
    try:
        object_id = ObjectId(point_id)
    except (InvalidId, TypeError):
        return None
    return await db[POINTS].find_one({"_id": object_id})


async def upsert_by_slug(db: AsyncDatabase, seed: PointSeed, now: datetime | None = None) -> bool:
    """Insere ou atualiza pelo slug. Devolve True se inseriu um ponto novo."""
    now = now or datetime.now(UTC)
    result = await db[POINTS].update_one(
        {"slug": seed.slug},
        {
            "$set": {**seed_to_document(seed), "updated_at": now},
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    return result.upserted_id is not None


async def find_existing_slugs(db: AsyncDatabase) -> set[str]:
    cursor = db[POINTS].find({}, {"slug": 1})
    return {document["slug"] async for document in cursor}


async def find_managed(db: AsyncDatabase) -> dict[str, bool]:
    """Pontos mantidos pelo admin: slug -> ativo."""
    cursor = db[POINTS].find({"managed_by": MANAGED_BY_ADMIN}, {"slug": 1, "active": 1})
    return {document["slug"]: bool(document.get("active")) async for document in cursor}


async def find_active_slugs_missing(db: AsyncDatabase, slugs: set[str]) -> list[str]:
    """Ativos fora de `slugs` (sem contar os mantidos pelo admin)."""
    cursor = db[POINTS].find(
        {"active": True, "slug": {"$nin": sorted(slugs)}, **NOT_MANAGED}, {"slug": 1}
    )
    return sorted([document["slug"] async for document in cursor])


async def deactivate_missing(
    db: AsyncDatabase, slugs: set[str], now: datetime | None = None
) -> int:
    """Desativa os pontos ativos fora de `slugs` (nunca os mantidos pelo admin)."""
    now = now or datetime.now(UTC)
    result = await db[POINTS].update_many(
        {"active": True, "slug": {"$nin": sorted(slugs)}, **NOT_MANAGED},
        {"$set": {"active": False, "updated_at": now}},
    )
    return result.modified_count


# --- painel de admin -------------------------------------------------------------------


async def count(db: AsyncDatabase, query: Document) -> int:
    return await db[POINTS].count_documents(query)


async def list_by_name(db: AsyncDatabase, query: Document, skip: int, limit: int) -> list[Document]:
    order = [("name", ASCENDING), ("_id", ASCENDING)]
    cursor = db[POINTS].find(query).sort(order).skip(skip).limit(limit)
    return await cursor.to_list(length=limit)


async def insert(db: AsyncDatabase, document: Document) -> ObjectId:
    result = await db[POINTS].insert_one(document)
    return result.inserted_id


async def update_by_id(db: AsyncDatabase, point_id: ObjectId, fields: Document) -> Document | None:
    return await db[POINTS].find_one_and_update(
        {"_id": point_id}, {"$set": fields}, return_document=ReturnDocument.AFTER
    )


async def slug_exists(db: AsyncDatabase, slug: str) -> bool:
    return await db[POINTS].count_documents({"slug": slug}, limit=1) > 0

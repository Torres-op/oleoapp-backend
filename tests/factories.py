"""Dados de teste compartilhados entre os arquivos da suíte."""

from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId
from fastapi import FastAPI

from app.db.collections import DISPOSALS, FAVORITES, POINTS

CENTER_LAT, CENTER_LNG = -22.9189, -42.8186  # centro de Maricá


def default_address() -> dict[str, Any]:
    return {
        "street": "Rua Exemplo",
        "number": "100",
        "bairro": "Centro",
        "city": "Maricá",
        "state": "RJ",
        "cep": "24900-000",
    }


def point_seed_data(slug: str, **overrides: Any) -> dict[str, Any]:
    """Um item válido de seed/collection_points.json (formato do PointSeed)."""
    data: dict[str, Any] = {
        "slug": slug,
        "name": f"EXEMPLO - {slug}",
        "address": default_address(),
        "location": {"lat": CENTER_LAT, "lng": CENTER_LNG},
        "phone": None,
        "opening_hours": None,
        "accepted_oils": ["motor_carro"],
        "conditions": None,
        "verified": True,
        "verified_at": "2026-10-01",
        "source": "teste",
        "google_place_id": None,
        "active": True,
    }
    data.update(overrides)
    return data


async def insert_point(
    app: FastAPI,
    name: str = "Ponto Exemplo",
    *,
    slug: str | None = None,
    lat: float = CENTER_LAT,
    lng: float = CENTER_LNG,
    active: bool = True,
    **overrides: Any,
) -> str:
    """Insere um ponto direto no banco (sem passar pelo seed) e devolve o id."""
    now = datetime.now(UTC)
    document: dict[str, Any] = {
        "slug": slug or f"ponto-{ObjectId()}",
        "name": name,
        "address": default_address(),
        "location": {"type": "Point", "coordinates": [lng, lat]},  # GeoJSON guarda [lng, lat]
        "accepted_oils": ["motor_carro", "motor_moto"],
        "verified": True,
        "active": active,
        "created_at": now,
        "updated_at": now,
    }
    document.update(overrides)
    result = await app.state.db[POINTS].insert_one(document)
    return str(result.inserted_id)


async def insert_disposal(
    app: FastAPI, user_id: str, *, volume_ml: int = 1000, age: timedelta = timedelta(0)
) -> str:
    result = await app.state.db[DISPOSALS].insert_one(
        {
            "user_id": ObjectId(user_id),
            "point_id": None,
            "point_name": None,
            "volume_ml": volume_ml,
            "created_at": datetime.now(UTC) - age,
        }
    )
    return str(result.inserted_id)


async def insert_favorite(
    app: FastAPI, user_id: str, point_id: str, *, age: timedelta = timedelta(0)
) -> None:
    await app.state.db[FAVORITES].insert_one(
        {
            "user_id": ObjectId(user_id),
            "point_id": ObjectId(point_id),
            "created_at": datetime.now(UTC) - age,
        }
    )

from typing import Any

from pymongo.asynchronous.database import AsyncDatabase

from app.core.errors import AppError
from app.repositories import points as points_repo
from app.schemas.points import (
    AddressOut,
    LocationOut,
    PointOut,
    PointSummaryOut,
    format_address,
)


def _point_not_found() -> AppError:
    return AppError(404, "not_found", "Ponto de coleta não encontrado.")


async def get_active_point(db: AsyncDatabase, point_id: str) -> dict[str, Any]:
    """Documento do ponto ativo com esse id. Inexistente, id inválido ou inativo: 404."""
    document = await points_repo.get_by_id(db, point_id)
    if document is None or not document.get("active", False):
        raise _point_not_found()
    return document


def _location(document: dict[str, Any]) -> LocationOut:
    lng, lat = document["location"]["coordinates"]  # GeoJSON guarda [lng, lat]
    return LocationOut(lat=lat, lng=lng)


def _text(value: Any) -> str | None:
    """Texto do documento, tolerando ausência e tipos inesperados (ex.: número salvo como int)."""
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        return None
    return str(value).strip() or None


def address_out(address: Any) -> AddressOut:
    """Endereço de saída a partir do documento, sem quebrar com dados fora do padrão."""
    if not isinstance(address, dict):
        address = {}
    street = _text(address.get("street"))
    number = _text(address.get("number"))
    bairro = _text(address.get("bairro"))
    city = _text(address.get("city"))
    state = _text(address.get("state"))
    cep = _text(address.get("cep"))
    return AddressOut(
        street=street or "",
        number=number,
        bairro=bairro or "",
        city=city or "",
        state=state or "",
        cep=cep,
        formatted=format_address(street, number, bairro, city, state, cep),
    )


def to_point_out(document: dict[str, Any]) -> PointOut:
    return PointOut(
        id=str(document["_id"]),
        slug=document["slug"],
        name=document["name"],
        address=address_out(document.get("address")),
        location=_location(document),
        phone=document.get("phone"),
        opening_hours=document.get("opening_hours"),
        accepted_oils=document["accepted_oils"],
        conditions=document.get("conditions"),
        verified=document["verified"],
        verified_at=document.get("verified_at"),
        google_place_id=document.get("google_place_id"),
        updated_at=document["updated_at"],
    )


def to_point_summary_out(document: dict[str, Any]) -> PointSummaryOut:
    address = address_out(document.get("address"))
    return PointSummaryOut(
        id=str(document["_id"]),
        name=document["name"],
        bairro=address.bairro,
        address_formatted=address.formatted,
        location=_location(document),
        accepted_oils=document["accepted_oils"],
        verified=document["verified"],
        distance_m=round(document["distance_m"]),
    )


async def find_near(
    db: AsyncDatabase, lat: float, lng: float, radius_m: int, limit: int
) -> list[PointSummaryOut]:
    documents = await points_repo.near(db, lat, lng, radius_m, limit)
    return [to_point_summary_out(document) for document in documents]


async def get_point(db: AsyncDatabase, point_id: str) -> PointOut:
    return to_point_out(await get_active_point(db, point_id))

from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.api.deps import CurrentUser
from app.core.errors import AppError
from app.core.impact_factors import IMPACT_FACTORS, IMPACT_SOURCES
from app.repositories import disposals as disposals_repo
from app.schemas.disposals import (
    DisposalImpactOut,
    DisposalIn,
    DisposalOut,
    DisposalPointOut,
    ImpactFactorsOut,
    ImpactOut,
    ImpactPeriod,
    ImpactSourcesOut,
    ReminderOut,
)
from app.services.points import get_active_point

PERIOD_DAYS: dict[str, int | None] = {"30d": 30, "6m": 182, "12m": 365, "all": None}
REMINDER_SOON_RATIO = 0.8


def disposal_impact(volume_ml: int) -> DisposalImpactOut:
    liters = volume_ml / 1000
    return DisposalImpactOut(
        water_l=float(round(liters * IMPACT_FACTORS["water_l_per_l_oluc"])),
        co2e_kg=round(liters * IMPACT_FACTORS["co2e_kg_per_l_oluc"], 2),
    )


def to_disposal_out(document: dict[str, Any]) -> DisposalOut:
    point_id = document.get("point_id")
    point = (
        DisposalPointOut(id=str(point_id), name=document.get("point_name") or "")
        if point_id is not None
        else None
    )
    return DisposalOut(
        id=str(document["_id"]),
        volume_ml=document["volume_ml"],
        point=point,
        created_at=document["created_at"],
        impact=disposal_impact(document["volume_ml"]),
    )


async def create_disposal(db: AsyncDatabase, user: CurrentUser, payload: DisposalIn) -> DisposalOut:
    point_id: ObjectId | None = None
    point_name: str | None = None
    if payload.point_id is not None:
        point = await get_active_point(db, payload.point_id)
        point_id = point["_id"]
        point_name = point["name"]  # cópia do nome no momento do registro

    document = await disposals_repo.create(
        db,
        user_id=ObjectId(user.id),
        point_id=point_id,
        point_name=point_name,
        volume_ml=payload.volume_ml,
        created_at=datetime.now(UTC),
    )
    return to_disposal_out(document)


async def list_disposals(db: AsyncDatabase, user: CurrentUser, limit: int) -> list[DisposalOut]:
    documents = await disposals_repo.list_by_user(db, ObjectId(user.id), limit)
    return [to_disposal_out(document) for document in documents]


async def delete_disposal(db: AsyncDatabase, user: CurrentUser, disposal_id: str) -> None:
    deleted = await disposals_repo.delete_by_user(db, ObjectId(user.id), disposal_id)
    if not deleted:
        raise AppError(404, "not_found", "Registro não encontrado.")


async def get_impact(db: AsyncDatabase, user: CurrentUser, period: ImpactPeriod) -> ImpactOut:
    days = PERIOD_DAYS[period]
    since = datetime.now(UTC) - timedelta(days=days) if days is not None else None
    total_ml, count = await disposals_repo.totals_by_user(db, ObjectId(user.id), since)

    liters = total_ml / 1000
    water_factor = IMPACT_FACTORS["water_l_per_l_oluc"]
    co2e_factor = IMPACT_FACTORS["co2e_kg_per_l_oluc"]
    return ImpactOut(
        period=period,
        total_ml=total_ml,
        count=count,
        water_l=round(liters * water_factor),
        co2e_kg=round(liters * co2e_factor, 1),
        factors_version=IMPACT_FACTORS["version"],
        factors=ImpactFactorsOut(
            water_l_per_l_oluc=water_factor,
            co2e_kg_per_l_oluc=co2e_factor,
        ),
        sources=ImpactSourcesOut(**IMPACT_SOURCES),
    )


def reminder_status(days_since: int, interval_days: int) -> str:
    if days_since >= interval_days:
        return "overdue"
    if days_since >= REMINDER_SOON_RATIO * interval_days:
        return "soon"
    return "ok"


async def get_reminder(db: AsyncDatabase, user: CurrentUser) -> ReminderOut:
    interval = user.reminder_interval_days
    latest = await disposals_repo.find_latest_by_user(db, ObjectId(user.id))
    if latest is None:
        return ReminderOut(
            status="none",
            last_disposal_at=None,
            days_since=None,
            days_left=None,
            reminder_interval_days=interval,
        )
    last_at: datetime = latest["created_at"]
    days_since = max(0, (datetime.now(UTC) - last_at).days)
    return ReminderOut(
        status=reminder_status(days_since, interval),
        last_disposal_at=last_at,
        days_since=days_since,
        days_left=interval - days_since,
        reminder_interval_days=interval,
    )

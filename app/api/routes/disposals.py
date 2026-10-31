from typing import Annotated

from fastapi import APIRouter, Query, Response

from app.api.deps import CurrentUserDep, Db, RequestCtx
from app.schemas.disposals import (
    DisposalIn,
    DisposalOut,
    DisposalsListOut,
    ImpactOut,
    ImpactPeriod,
    ReminderOut,
)
from app.services import audit
from app.services import disposals as disposals_service

router = APIRouter(prefix="/disposals", tags=["disposals"])


@router.post("", response_model=DisposalOut, status_code=201)
async def create_disposal(
    payload: DisposalIn, db: Db, user: CurrentUserDep, ctx: RequestCtx
) -> DisposalOut:
    disposal = await disposals_service.create_disposal(db, user, payload)
    await audit.record(db, "disposal_created", ctx, user_id=user.id, email=user.email)
    return disposal


@router.get("", response_model=DisposalsListOut)
async def list_disposals(
    db: Db,
    user: CurrentUserDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> DisposalsListOut:
    items = await disposals_service.list_disposals(db, user, limit)
    return DisposalsListOut(items=items)


# "/impact" e "/reminder" vêm antes de "/{disposal_id}" para não serem confundidos com um id.
@router.get("/impact", response_model=ImpactOut)
async def get_impact(
    db: Db,
    user: CurrentUserDep,
    period: ImpactPeriod = "30d",
) -> ImpactOut:
    return await disposals_service.get_impact(db, user, period)


@router.get("/reminder", response_model=ReminderOut)
async def get_reminder(db: Db, user: CurrentUserDep) -> ReminderOut:
    return await disposals_service.get_reminder(db, user)


@router.delete("/{disposal_id}", status_code=204)
async def delete_disposal(disposal_id: str, db: Db, user: CurrentUserDep) -> Response:
    await disposals_service.delete_disposal(db, user, disposal_id)
    return Response(status_code=204)

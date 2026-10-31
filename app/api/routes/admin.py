from datetime import UTC, date, datetime, timedelta
from typing import Annotated, Any

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.exceptions import RequestValidationError

from app.api.deps import AdminUserDep, Db, RedisDep, RequestCtx, admin_rate_limit
from app.schemas.admin import (
    AdminPointIn,
    AdminPointOut,
    AdminPointPatch,
    AdminPointsPage,
    AdminUserOut,
    AdminUsersPage,
    AlertsOut,
    AuditLogsPage,
    DeactivateIn,
    OverviewOut,
    SystemStatusOut,
    TimeseriesOut,
)
from app.services import admin as admin_service
from app.services import audit
from app.services.audit import EventType

# Toda rota daqui exige admin (require_admin, dentro de admin_rate_limit) e conta no limite
# por administrador. A checagem é sempre no servidor, com o papel lido do banco.
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(admin_rate_limit)])

TIMESERIES_DAYS = (7, 30, 90)
MAX_PAGE = 10_000


def _invalid(field: str, value: str, message: str) -> RequestValidationError:
    return RequestValidationError(
        [
            {
                "type": "value_error",
                "loc": ("query", field),
                "msg": f"Value error, {message}",
                "input": value,
            }
        ]
    )


def _parse_moment(value: str, field: str, *, end_of_day: bool) -> datetime:
    """Data (YYYY-MM-DD, dia inteiro em São Paulo) ou data e hora ISO (sem fuso = UTC)."""
    try:
        if len(value) == 10:
            start = admin_service.local_day_start(date.fromisoformat(value))
            return start + timedelta(days=1, milliseconds=-1) if end_of_day else start
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise _invalid(field, value, "Data inválida. Use AAAA-MM-DD.") from None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


@router.get("/metrics/overview", response_model=OverviewOut)
async def metrics_overview(db: Db) -> OverviewOut:
    return await admin_service.overview(db)


@router.get("/metrics/timeseries", response_model=TimeseriesOut)
async def metrics_timeseries(db: Db, days: int = 30) -> TimeseriesOut:
    if days not in TIMESERIES_DAYS:
        raise _invalid("days", str(days), "Use 7, 30 ou 90 dias.")
    return await admin_service.timeseries(db, days)


@router.get("/users", response_model=AdminUsersPage)
async def list_users(
    request: Request,
    db: Db,
    admin: AdminUserDep,
    ctx: RequestCtx,
    q: Annotated[str | None, Query(max_length=100)] = None,
    status: admin_service.UserStatus = "all",
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AdminUsersPage:
    # Consulta a dados pessoais: fica registrada na auditoria.
    await audit.record(
        db, "admin_access", ctx, user_id=admin.id, email=admin.email, reason=request.url.path
    )
    return await admin_service.list_users(db, q, status, page, page_size)


@router.post("/users/{user_id}/deactivate", response_model=AdminUserOut)
async def deactivate_user(
    user_id: str, data: DeactivateIn, db: Db, admin: AdminUserDep, ctx: RequestCtx
) -> AdminUserOut:
    return await admin_service.deactivate_user(db, admin, user_id, data.reason, ctx)


@router.post("/users/{user_id}/reactivate", response_model=AdminUserOut)
async def reactivate_user(
    user_id: str, db: Db, admin: AdminUserDep, ctx: RequestCtx
) -> AdminUserOut:
    return await admin_service.reactivate_user(db, admin, user_id, ctx)


async def log_filters(
    event_type: Annotated[EventType | None, Query(alias="type")] = None,
    success: bool | None = None,
    user_id: str | None = None,
    date_from: Annotated[str | None, Query(alias="from", max_length=40)] = None,
    date_to: Annotated[str | None, Query(alias="to", max_length=40)] = None,
) -> dict[str, Any]:
    """Filtros dos logs, iguais na listagem e na exportação CSV."""
    user_oid: ObjectId | None = None
    if user_id:
        try:
            user_oid = ObjectId(user_id)
        except (InvalidId, TypeError):
            raise _invalid("user_id", user_id, "Id de usuário inválido.") from None
    since = _parse_moment(date_from, "from", end_of_day=False) if date_from else None
    until = _parse_moment(date_to, "to", end_of_day=True) if date_to else None
    return admin_service.logs_query(event_type, success, user_oid, since, until)


LogFilters = Annotated[dict[str, Any], Depends(log_filters)]


@router.get("/logs", response_model=AuditLogsPage)
async def list_logs(
    request: Request,
    db: Db,
    admin: AdminUserDep,
    ctx: RequestCtx,
    query: LogFilters,
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 50,
) -> AuditLogsPage:
    await audit.record(
        db, "admin_access", ctx, user_id=admin.id, email=admin.email, reason=request.url.path
    )
    return await admin_service.list_logs(db, query, page, page_size)


@router.get("/logs/export")
async def export_logs(
    request: Request, db: Db, admin: AdminUserDep, ctx: RequestCtx, query: LogFilters
) -> Response:
    """CSV dos logs com os mesmos filtros da listagem (até ADMIN_EXPORT_MAX_ROWS linhas)."""
    await audit.record(
        db, "admin_access", ctx, user_id=admin.id, email=admin.email, reason=request.url.path
    )
    content, truncated = await admin_service.export_logs_csv(db, query)
    headers = {
        "Content-Disposition": f'attachment; filename="{admin_service.export_filename()}"',
        "Cache-Control": "no-store",
    }
    if truncated:
        headers["X-Export-Truncated"] = "true"
    return Response(content=content, media_type="text/csv; charset=utf-8", headers=headers)


@router.get("/alerts", response_model=AlertsOut)
async def alerts(db: Db) -> AlertsOut:
    return await admin_service.login_alerts(db)


@router.get("/points", response_model=AdminPointsPage)
async def list_points(
    db: Db,
    q: Annotated[str | None, Query(max_length=100)] = None,
    status: admin_service.PointStatus = "all",
    page: Annotated[int, Query(ge=1, le=MAX_PAGE)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> AdminPointsPage:
    return await admin_service.list_points(db, q, status, page, page_size)


@router.get("/points/{point_id}", response_model=AdminPointOut)
async def get_point(point_id: str, db: Db) -> AdminPointOut:
    return await admin_service.get_point(db, point_id)


@router.post("/points", response_model=AdminPointOut, status_code=201)
async def create_point(
    data: AdminPointIn, db: Db, admin: AdminUserDep, ctx: RequestCtx
) -> AdminPointOut:
    return await admin_service.create_point(db, admin, data, ctx)


@router.patch("/points/{point_id}", response_model=AdminPointOut)
async def update_point(
    point_id: str, data: AdminPointPatch, db: Db, admin: AdminUserDep, ctx: RequestCtx
) -> AdminPointOut:
    return await admin_service.update_point(db, admin, point_id, data, ctx)


@router.get("/system/status", response_model=SystemStatusOut)
async def system_status(request: Request, db: Db, redis: RedisDep) -> SystemStatusOut:
    return await admin_service.system_status(db, redis, request.app.state.started_at)

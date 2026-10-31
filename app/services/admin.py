"""Painel de administração: métricas, listagens e status do sistema.

Tudo com agregações e contagens no MongoDB (nada de carregar coleções inteiras). Os dias
("hoje", série diária) seguem o fuso de São Paulo. Nunca expõe hash de senha, ids de login
social nem chaves de integrações.
"""

import csv
import io
import re
import secrets
import time
import unicodedata
from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from datetime import time as dtime
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import DESCENDING
from pymongo.asynchronous.database import AsyncDatabase
from redis.asyncio import Redis

from app.api.deps import CurrentUser
from app.core.config import APP_VERSION, settings
from app.core.errors import AppError
from app.core.impact_factors import IMPACT_FACTORS
from app.db.collections import (
    AUDIT_LOGS,
    DISPOSALS,
    FAVORITES,
    POINTS,
    SESSIONS,
    USERS,
)
from app.repositories import audit as audit_repo
from app.repositories import oauth as oauth_repo
from app.repositories import points as points_repo
from app.repositories import sessions as sessions_repo
from app.repositories import users as users_repo
from app.schemas.admin import (
    AdminPointIn,
    AdminPointOut,
    AdminPointPatch,
    AdminPointsPage,
    AdminUserOut,
    AdminUsersPage,
    AlertsOut,
    ApiStatus,
    AuditLogOut,
    AuditLogsPage,
    CollectionStatus,
    DatabaseStatus,
    DisposalsMetrics,
    ImpactMetrics,
    IntegrationsStatus,
    LoginAlert,
    LoginsMetrics,
    OverviewOut,
    RedisStatus,
    SeriesDay,
    SystemStatusOut,
    TimeseriesOut,
    TopPoint,
    UsersMetrics,
)
from app.schemas.points import LocationOut
from app.services import audit
from app.services import oauth as oauth_service
from app.services.points import address_out

TZ_NAME = "America/Sao_Paulo"
TZ: tzinfo
try:
    TZ = ZoneInfo(TZ_NAME)
except ZoneInfoNotFoundError:
    # Imagem sem tzdata: São Paulo fica em UTC-3 o ano todo (sem horário de verão).
    TZ = timezone(timedelta(hours=-3), TZ_NAME)

UserStatus = Literal["all", "active", "inactive", "admin", "no_password", "disabled"]
PointStatus = Literal["all", "active", "inactive"]
ACTIVE_DAYS = 30
TOP_POINTS = 5
STATUS_COLLECTIONS = (USERS, SESSIONS, POINTS, DISPOSALS, FAVORITES, AUDIT_LOGS)


def today_local() -> date:
    return datetime.now(TZ).date()


def local_day_start(day: date) -> datetime:
    """Início do dia `day` em São Paulo, convertido para UTC."""
    return datetime.combine(day, dtime.min, tzinfo=TZ).astimezone(UTC)


def _liters(ml: float) -> float:
    return round(ml / 1000, 2)


async def _aggregate(db: AsyncDatabase, collection: str, pipeline: list[dict]) -> list[dict]:
    cursor = await db[collection].aggregate(pipeline)
    return await cursor.to_list(length=None)


def _day_expr(field: str = "$created_at") -> dict[str, Any]:
    return {"$dateToString": {"format": "%Y-%m-%d", "date": field, "timezone": TZ_NAME}}


# --- métricas -----------------------------------------------------------------------


async def overview(db: AsyncDatabase) -> OverviewOut:
    now = datetime.now(UTC)
    today_start = local_day_start(today_local())
    since_7d = now - timedelta(days=7)
    since_30d = now - timedelta(days=30)

    def created_since(since: datetime) -> dict[str, Any]:
        return {"created_at": {"$gte": since}}

    users_col = db[USERS]
    users = UsersMetrics(
        total=await users_col.count_documents({}),
        new_today=await users_col.count_documents(created_since(today_start)),
        new_7d=await users_col.count_documents(created_since(since_7d)),
        new_30d=await users_col.count_documents(created_since(since_30d)),
        active_30d=await users_col.count_documents({"last_login_at": {"$gte": since_30d}}),
        admins=await users_col.count_documents({"role": "admin"}),
    )

    async def audit_count(event_type: str, since: datetime) -> int:
        return await audit_repo.count(db, {"type": event_type, **created_since(since)})

    logins = LoginsMetrics(
        success_today=await audit_count("login_success", today_start),
        failed_today=await audit_count("login_failure", today_start),
        success_7d=await audit_count("login_success", since_7d),
        failed_7d=await audit_count("login_failure", since_7d),
    )

    totals_group = {"$group": {"_id": None, "n": {"$sum": 1}, "ml": {"$sum": "$volume_ml"}}}
    facet = await _aggregate(
        db,
        DISPOSALS,
        [
            {
                "$facet": {
                    "all": [totals_group],
                    "recent": [{"$match": created_since(since_30d)}, totals_group],
                }
            }
        ],
    )
    all_rows = facet[0]["all"] if facet else []
    recent_rows = facet[0]["recent"] if facet else []
    total_n = int(all_rows[0]["n"]) if all_rows else 0
    total_ml = float(all_rows[0]["ml"]) if all_rows else 0.0
    recent_n = int(recent_rows[0]["n"]) if recent_rows else 0
    recent_ml = float(recent_rows[0]["ml"]) if recent_rows else 0.0

    top_rows = await _aggregate(
        db,
        DISPOSALS,
        [
            {"$match": {"point_id": {"$ne": None}}},
            {"$sort": {"created_at": DESCENDING}},
            {
                "$group": {
                    "_id": "$point_id",
                    "name": {"$first": "$point_name"},
                    "n": {"$sum": 1},
                    "ml": {"$sum": "$volume_ml"},
                }
            },
            {"$sort": {"n": DESCENDING, "ml": DESCENDING}},
            {"$limit": TOP_POINTS},
        ],
    )

    total_liters = total_ml / 1000
    return OverviewOut(
        users=users,
        logins=logins,
        accounts_deleted_30d=await audit_count("account_deleted", since_30d),
        disposals=DisposalsMetrics(
            total=total_n,
            total_liters=_liters(total_ml),
            last_30d=recent_n,
            liters_30d=_liters(recent_ml),
        ),
        impact=ImpactMetrics(
            water_l=float(round(total_liters * IMPACT_FACTORS["water_l_per_l_oluc"])),
            co2e_kg=round(total_liters * IMPACT_FACTORS["co2e_kg_per_l_oluc"], 1),
        ),
        top_points=[
            TopPoint(
                id=str(row["_id"]),
                name=row.get("name") or "",
                disposals=int(row["n"]),
                liters=_liters(row["ml"]),
            )
            for row in top_rows
        ],
        generated_at=now,
    )


async def timeseries(db: AsyncDatabase, days: int) -> TimeseriesOut:
    today = today_local()
    start = today - timedelta(days=days - 1)
    since = local_day_start(start)
    match = {"$match": {"created_at": {"$gte": since}}}

    signups = {
        row["_id"]: int(row["n"])
        for row in await _aggregate(
            db, USERS, [match, {"$group": {"_id": _day_expr(), "n": {"$sum": 1}}}]
        )
    }
    login_rows = await audit_repo.aggregate(
        db,
        [
            {
                "$match": {
                    "type": {"$in": ["login_success", "login_failure"]},
                    "created_at": {"$gte": since},
                }
            },
            {"$group": {"_id": {"day": _day_expr(), "type": "$type"}, "n": {"$sum": 1}}},
        ],
    )
    logins: dict[tuple[str, str], int] = {
        (row["_id"]["day"], row["_id"]["type"]): int(row["n"]) for row in login_rows
    }
    disposals = {
        row["_id"]: (int(row["n"]), float(row["ml"]))
        for row in await _aggregate(
            db,
            DISPOSALS,
            [
                match,
                {
                    "$group": {
                        "_id": _day_expr(),
                        "n": {"$sum": 1},
                        "ml": {"$sum": "$volume_ml"},
                    }
                },
            ],
        )
    }

    series: list[SeriesDay] = []
    for offset in range(days):
        key = (start + timedelta(days=offset)).isoformat()
        count, ml = disposals.get(key, (0, 0.0))
        series.append(
            SeriesDay(
                date=key,
                signups=signups.get(key, 0),
                logins=logins.get((key, "login_success"), 0),
                login_failures=logins.get((key, "login_failure"), 0),
                disposals=count,
                liters=_liters(ml),
            )
        )
    return TimeseriesOut(days=days, series=series)


# --- usuários ---------------------------------------------------------------------------


def users_query(q: str | None, status: UserStatus, now: datetime) -> dict[str, Any]:
    clauses: list[dict[str, Any]] = []
    if q:
        pattern = {"$regex": re.escape(q.strip()), "$options": "i"}
        clauses.append({"$or": [{"name": pattern}, {"email": pattern}]})
    cutoff = now - timedelta(days=ACTIVE_DAYS)
    if status == "active":
        clauses.append({"last_login_at": {"$gte": cutoff}})
    elif status == "inactive":
        # Sem login nos últimos 30 dias (inclui quem nunca entrou desde a auditoria).
        clauses.append({"last_login_at": {"$not": {"$gte": cutoff}}})
    elif status == "admin":
        clauses.append({"role": "admin"})
    elif status == "no_password":
        clauses.append({"password_hash": None})
    elif status == "disabled":
        clauses.append({"disabled_at": {"$ne": None}})
    if not clauses:
        return {}
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


async def _user_items(
    db: AsyncDatabase, query: dict[str, Any], skip: int, limit: int
) -> list[AdminUserOut]:
    rows = await _aggregate(
        db,
        USERS,
        [
            {"$match": query},
            {"$sort": {"created_at": DESCENDING, "_id": DESCENDING}},
            {"$skip": skip},
            {"$limit": limit},
            # Contagem de descartes só dos usuários desta página (usa o índice user_id).
            {
                "$lookup": {
                    "from": DISPOSALS,
                    "let": {"uid": "$_id"},
                    "pipeline": [
                        {"$match": {"$expr": {"$eq": ["$user_id", "$$uid"]}}},
                        {"$count": "n"},
                    ],
                    "as": "disposals",
                }
            },
            # Só o necessário: o hash da senha e os ids do login social não saem do banco.
            {
                "$project": {
                    "name": 1,
                    "email": 1,
                    "role": 1,
                    "bairro": 1,
                    "created_at": 1,
                    "last_login_at": 1,
                    "has_password": {"$gt": ["$password_hash", None]},
                    "google": {"$gt": ["$oauth.google", None]},
                    "disposals_count": {"$ifNull": [{"$first": "$disposals.n"}, 0]},
                    "disabled_at": 1,
                }
            },
        ],
    )
    return [
        AdminUserOut(
            id=str(row["_id"]),
            name=row["name"],
            email=row["email"],
            role="admin" if row.get("role") == "admin" else "user",
            bairro=row.get("bairro"),
            created_at=row["created_at"],
            last_login_at=row.get("last_login_at"),
            has_password=bool(row.get("has_password")),
            providers=["google"] if row.get("google") else [],
            disposals_count=int(row.get("disposals_count") or 0),
            disabled=row.get("disabled_at") is not None,
            disabled_at=row.get("disabled_at"),
        )
        for row in rows
    ]


async def list_users(
    db: AsyncDatabase, q: str | None, status: UserStatus, page: int, page_size: int
) -> AdminUsersPage:
    query = users_query(q, status, datetime.now(UTC))
    total = await db[USERS].count_documents(query)
    items = await _user_items(db, query, (page - 1) * page_size, page_size)
    return AdminUsersPage(items=items, page=page, page_size=page_size, total=total)


def _parse_object_id(value: str) -> ObjectId | None:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def _user_not_found() -> AppError:
    return AppError(404, "not_found", "Usuário não encontrado.")


async def _admin_user_item(db: AsyncDatabase, user_id: ObjectId) -> AdminUserOut:
    items = await _user_items(db, {"_id": user_id}, 0, 1)
    if not items:
        raise _user_not_found()
    return items[0]


async def _find_user(db: AsyncDatabase, user_id: str) -> dict[str, Any]:
    oid = _parse_object_id(user_id)
    document = await users_repo.find_by_id(db, oid) if oid is not None else None
    if document is None:
        raise _user_not_found()
    return document


async def deactivate_user(
    db: AsyncDatabase,
    admin: CurrentUser,
    user_id: str,
    reason: str | None,
    ctx: audit.RequestContext,
) -> AdminUserOut:
    """Desativa a conta: derruba as sessões e impede novos logins (senha e Google)."""
    document = await _find_user(db, user_id)
    if str(document["_id"]) == admin.id:
        raise AppError(400, "cannot_deactivate_self", "Você não pode desativar a própria conta.")
    if document.get("role") == "admin":
        raise AppError(
            400, "cannot_deactivate_admin", "Contas de administrador não podem ser desativadas."
        )
    if document.get("disabled_at") is None:
        now = datetime.now(UTC)
        await users_repo.update_fields(
            db,
            document["_id"],
            {"disabled_at": now, "disabled_reason": reason or None, "updated_at": now},
        )
        await sessions_repo.delete_by_user(db, document["_id"])
        await oauth_repo.delete_pending_links_by_user(db, document["_id"])
        await audit.record(
            db,
            "admin_user_deactivated",
            ctx,
            user_id=admin.id,
            email=admin.email,
            reason=reason or None,
            target_id=document["_id"],
        )
    return await _admin_user_item(db, document["_id"])


async def reactivate_user(
    db: AsyncDatabase, admin: CurrentUser, user_id: str, ctx: audit.RequestContext
) -> AdminUserOut:
    document = await _find_user(db, user_id)
    if document.get("disabled_at") is not None:
        await users_repo.update_fields(
            db,
            document["_id"],
            {"disabled_at": None, "disabled_reason": None, "updated_at": datetime.now(UTC)},
        )
        await audit.record(
            db,
            "admin_user_reactivated",
            ctx,
            user_id=admin.id,
            email=admin.email,
            target_id=document["_id"],
        )
    return await _admin_user_item(db, document["_id"])


# --- logs ---------------------------------------------------------------------------------


def logs_query(
    event_type: str | None,
    success: bool | None,
    user_id: ObjectId | None,
    since: datetime | None,
    until: datetime | None,
) -> dict[str, Any]:
    query: dict[str, Any] = {}
    if event_type:
        query["type"] = event_type
    if success is not None:
        query["success"] = success
    if user_id is not None:
        query["user_id"] = user_id
    if since is not None or until is not None:
        query["created_at"] = {
            **({"$gte": since} if since is not None else {}),
            **({"$lte": until} if until is not None else {}),
        }
    return query


async def list_logs(
    db: AsyncDatabase, query: dict[str, Any], page: int, page_size: int
) -> AuditLogsPage:
    total = await audit_repo.count(db, query)
    rows = await audit_repo.list_page(db, query, (page - 1) * page_size, page_size)
    items = [
        AuditLogOut(
            id=str(row["_id"]),
            created_at=row["created_at"],
            type=row["type"],
            user_id=str(row["user_id"]) if row.get("user_id") is not None else None,
            email=row.get("email"),
            ip=row.get("ip"),
            user_agent=row.get("user_agent"),
            success=bool(row.get("success")),
            reason=row.get("reason"),
            method=row.get("method"),
            target_id=row.get("target_id"),
        )
        for row in rows
    ]
    return AuditLogsPage(items=items, page=page, page_size=page_size, total=total)


CSV_HEADER = (
    "data_hora",
    "tipo",
    "sucesso",
    "usuario_id",
    "email",
    "ip",
    "navegador",
    "motivo",
    "metodo",
    "alvo_id",
)
# Planilhas executam como fórmula a célula que começa com esses caracteres.
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return f"'{text}" if text.startswith(FORMULA_PREFIXES) else text


def _local_iso(moment: datetime) -> str:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(TZ).isoformat(timespec="seconds")


async def export_logs_csv(db: AsyncDatabase, query: dict[str, Any]) -> tuple[str, bool]:
    """CSV dos logs do filtro, com BOM (para o Excel). Devolve (conteúdo, foi cortado)."""
    limit = settings.ADMIN_EXPORT_MAX_ROWS
    rows = await audit_repo.list_page(db, query, 0, limit + 1)
    truncated = len(rows) > limit
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_HEADER)
    for row in rows[:limit]:
        user_id = row.get("user_id")
        writer.writerow(
            [
                _csv_cell(_local_iso(row["created_at"])),
                _csv_cell(row.get("type")),
                "sim" if row.get("success") else "nao",
                _csv_cell(str(user_id) if user_id is not None else None),
                _csv_cell(row.get("email")),
                _csv_cell(row.get("ip")),
                _csv_cell(row.get("user_agent")),
                _csv_cell(row.get("reason")),
                _csv_cell(row.get("method")),
                _csv_cell(row.get("target_id")),
            ]
        )
    return "\ufeff" + buffer.getvalue(), truncated


def export_filename() -> str:
    return f"logs-oleoapp-{datetime.now(TZ).strftime('%Y%m%d-%H%M')}.csv"


# --- alertas ----------------------------------------------------------------------------

MAX_ALERTS = 20


async def login_alerts(db: AsyncDatabase) -> AlertsOut:
    """Falhas de login na janela, por IP e (separado) por e-mail mascarado, acima do limite."""
    now = datetime.now(UTC)
    window = settings.LOGIN_FAILURE_ALERT_WINDOW_MIN
    threshold = settings.LOGIN_FAILURE_ALERT_THRESHOLD

    def grouped_by(field: str) -> list[dict[str, Any]]:
        return [
            {"$match": {field: {"$nin": [None, ""]}}},
            {
                "$group": {
                    "_id": f"${field}",
                    "failures": {"$sum": 1},
                    "first_at": {"$min": "$created_at"},
                    "last_at": {"$max": "$created_at"},
                }
            },
            {"$match": {"failures": {"$gte": threshold}}},
        ]

    facet = await audit_repo.aggregate(
        db,
        [
            {
                "$match": {
                    "type": "login_failure",
                    "created_at": {"$gte": now - timedelta(minutes=window)},
                }
            },
            {"$facet": {"ip": grouped_by("ip"), "email": grouped_by("email")}},
        ],
    )
    groups = facet[0] if facet else {}
    items = [
        LoginAlert(
            kind=kind,
            key=str(row["_id"]),
            failures=int(row["failures"]),
            first_at=row["first_at"],
            last_at=row["last_at"],
        )
        for kind in ("ip", "email")
        for row in groups.get(kind, [])
    ]
    items.sort(key=lambda item: (item.failures, item.last_at), reverse=True)
    return AlertsOut(
        window_min=window, threshold=threshold, generated_at=now, items=items[:MAX_ALERTS]
    )


# --- pontos de coleta ---------------------------------------------------------------------


def points_query(q: str | None, status: PointStatus) -> dict[str, Any]:
    clauses: list[dict[str, Any]] = []
    if q and q.strip():
        pattern = {"$regex": re.escape(q.strip()), "$options": "i"}
        clauses.append(
            {"$or": [{"name": pattern}, {"address.bairro": pattern}, {"address.street": pattern}]}
        )
    if status == "active":
        clauses.append({"active": True})
    elif status == "inactive":
        clauses.append({"active": {"$ne": True}})
    if not clauses:
        return {}
    return clauses[0] if len(clauses) == 1 else {"$and": clauses}


def to_admin_point(document: dict[str, Any]) -> AdminPointOut:
    coordinates = (document.get("location") or {}).get("coordinates") or [0.0, 0.0]
    return AdminPointOut(
        id=str(document["_id"]),
        slug=document["slug"],
        name=document["name"],
        address=address_out(document.get("address")),
        location=LocationOut(lat=coordinates[1], lng=coordinates[0]),
        phone=document.get("phone"),
        opening_hours=document.get("opening_hours"),
        accepted_oils=list(document.get("accepted_oils") or []),
        conditions=document.get("conditions"),
        verified=bool(document.get("verified")),
        verified_at=document.get("verified_at"),
        google_place_id=document.get("google_place_id"),
        active=bool(document.get("active")),
        source=document.get("source"),
        managed_by=document.get("managed_by"),
        created_at=document.get("created_at"),
        updated_at=document.get("updated_at"),
    )


def _point_not_found() -> AppError:
    return AppError(404, "not_found", "Ponto de coleta não encontrado.")


async def list_points(
    db: AsyncDatabase, q: str | None, status: PointStatus, page: int, page_size: int
) -> AdminPointsPage:
    query = points_query(q, status)
    total = await points_repo.count(db, query)
    rows = await points_repo.list_by_name(db, query, (page - 1) * page_size, page_size)
    return AdminPointsPage(
        items=[to_admin_point(row) for row in rows], page=page, page_size=page_size, total=total
    )


async def get_point(db: AsyncDatabase, point_id: str) -> AdminPointOut:
    document = await points_repo.get_by_id(db, point_id)
    if document is None:
        raise _point_not_found()
    return to_admin_point(document)


def _slugify(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", plain).strip("-")[:50].strip("-") or "ponto"


async def _new_slug(db: AsyncDatabase, name: str) -> str:
    base = _slugify(name)
    while True:
        slug = f"admin-{base}-{secrets.token_hex(3)}"
        if not await points_repo.slug_exists(db, slug):
            return slug


def _today_utc() -> datetime:
    today = today_local()
    return datetime(today.year, today.month, today.day, tzinfo=UTC)


def _location_doc(lat: float, lng: float) -> dict[str, Any]:
    return {"type": "Point", "coordinates": [lng, lat]}  # GeoJSON guarda [lng, lat]


async def create_point(
    db: AsyncDatabase, admin: CurrentUser, data: AdminPointIn, ctx: audit.RequestContext
) -> AdminPointOut:
    now = datetime.now(UTC)
    document: dict[str, Any] = {
        "slug": await _new_slug(db, data.name),
        "name": data.name,
        "address": data.address.model_dump(),
        "location": _location_doc(data.location.lat, data.location.lng),
        "phone": data.phone,
        "opening_hours": data.opening_hours,
        "accepted_oils": [oil.value for oil in data.accepted_oils],
        "conditions": data.conditions,
        "verified": data.verified,
        "verified_at": _today_utc() if data.verified else None,
        "source": "admin",
        "google_place_id": None,
        "active": data.active,
        "managed_by": points_repo.MANAGED_BY_ADMIN,
        "created_at": now,
        "updated_at": now,
    }
    document["_id"] = await points_repo.insert(db, document)
    await audit.record(
        db,
        "admin_point_created",
        ctx,
        user_id=admin.id,
        email=admin.email,
        target_id=document["_id"],
    )
    return to_admin_point(document)


async def update_point(
    db: AsyncDatabase,
    admin: CurrentUser,
    point_id: str,
    data: AdminPointPatch,
    ctx: audit.RequestContext,
) -> AdminPointOut:
    document = await points_repo.get_by_id(db, point_id)
    if document is None:
        raise _point_not_found()

    changed = [field for field in AdminPointPatch.model_fields if field in data.model_fields_set]
    fields: dict[str, Any] = {}
    for field in changed:
        value = getattr(data, field)
        if field == "address":
            fields["address"] = value.model_dump()
        elif field == "location":
            fields["location"] = _location_doc(value.lat, value.lng)
        elif field == "accepted_oils":
            fields["accepted_oils"] = [oil.value for oil in value]
        elif field == "verified":
            fields["verified"] = value
            if value and not document.get("verified"):
                fields["verified_at"] = _today_utc()
            elif not value:
                fields["verified_at"] = None
        else:
            fields[field] = value
    fields["managed_by"] = points_repo.MANAGED_BY_ADMIN
    fields["updated_at"] = datetime.now(UTC)

    updated = await points_repo.update_by_id(db, document["_id"], fields)
    if updated is None:
        raise _point_not_found()
    await audit.record(
        db,
        "admin_point_updated",
        ctx,
        user_id=admin.id,
        email=admin.email,
        reason=",".join(changed) or None,
        target_id=document["_id"],
    )
    return to_admin_point(updated)


# --- status do sistema -----------------------------------------------------------------------


async def _collection_status(db: AsyncDatabase, name: str) -> CollectionStatus:
    try:
        count: int | None = await db[name].estimated_document_count()
    except Exception:
        count = None
    size: int | None = None
    try:
        # O Atlas M0 pode negar $collStats: nesse caso o tamanho fica nulo.
        rows = await _aggregate(db, name, [{"$collStats": {"storageStats": {}}}])
        if rows:
            size = int(rows[0]["storageStats"]["size"])
    except Exception:
        size = None
    return CollectionStatus(name=name, count=count, size_bytes=size)


async def system_status(
    db: AsyncDatabase, redis: Redis | None, started_at: datetime
) -> SystemStatusOut:
    now = datetime.now(UTC)

    latency_ms: float | None = None
    db_ok = False
    try:
        start = time.perf_counter()
        await db.command("ping")
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        db_ok = True
    except Exception:
        db_ok = False
    collections = (
        [await _collection_status(db, name) for name in STATUS_COLLECTIONS] if db_ok else []
    )

    redis_ok = False
    if redis is not None:
        try:
            redis_ok = bool(await redis.ping())
        except Exception:
            redis_ok = False

    return SystemStatusOut(
        api=ApiStatus(
            version=APP_VERSION,
            env=settings.ENV,
            started_at=started_at,
            uptime_s=max(0, int((now - started_at).total_seconds())),
            server_time=now,
        ),
        database=DatabaseStatus(ok=db_ok, latency_ms=latency_ms, collections=collections),
        redis=RedisStatus(configured=bool(settings.REDIS_URL), ok=redis_ok),
        # Só "configurado ou não": nenhuma chave sai daqui.
        integrations=IntegrationsStatus(
            google_maps=bool(settings.GOOGLE_MAPS_API_KEY),
            google_login=oauth_service.is_configured("google"),
            email_provider=settings.EMAIL_PROVIDER,
            email_configured=settings.EMAIL_PROVIDER == "brevo"
            and bool(settings.BREVO_API_KEY)
            and bool(settings.EMAIL_FROM),
            geocoder=settings.GEOCODER,
        ),
    )

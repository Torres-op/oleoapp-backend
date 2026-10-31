from datetime import date, datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator

from app.core.geo import within_area
from app.schemas.common import LatLng
from app.schemas.points import (
    AddressOut,
    Cep,
    LocationOut,
    OilType,
    PointName,
    StateCode,
    StrictModel,
    Text20,
    Text30,
    Text60,
    Text120,
    Text200,
    Text300,
)

# --- métricas -------------------------------------------------------------------


class UsersMetrics(BaseModel):
    total: int
    new_today: int
    new_7d: int
    new_30d: int
    active_30d: int
    admins: int


class LoginsMetrics(BaseModel):
    success_today: int
    failed_today: int
    success_7d: int
    failed_7d: int


class DisposalsMetrics(BaseModel):
    total: int
    total_liters: float
    last_30d: int
    liters_30d: float


class ImpactMetrics(BaseModel):
    water_l: float
    co2e_kg: float


class TopPoint(BaseModel):
    id: str
    name: str
    disposals: int
    liters: float


class OverviewOut(BaseModel):
    users: UsersMetrics
    logins: LoginsMetrics
    accounts_deleted_30d: int
    disposals: DisposalsMetrics
    impact: ImpactMetrics
    top_points: list[TopPoint]
    generated_at: datetime


class SeriesDay(BaseModel):
    date: str  # YYYY-MM-DD, no fuso de São Paulo
    signups: int
    logins: int
    login_failures: int
    disposals: int
    liters: float


class TimeseriesOut(BaseModel):
    days: int
    series: list[SeriesDay]


# --- usuários e logs --------------------------------------------------------------


class AdminUserOut(BaseModel):
    id: str
    name: str
    email: str
    role: str
    bairro: str | None
    created_at: datetime
    last_login_at: datetime | None
    has_password: bool
    providers: list[str]
    disposals_count: int
    disabled: bool
    disabled_at: datetime | None


class AdminUsersPage(BaseModel):
    items: list[AdminUserOut]
    page: int
    page_size: int
    total: int


class AuditLogOut(BaseModel):
    id: str
    created_at: datetime
    type: str
    user_id: str | None
    email: str | None
    ip: str | None
    user_agent: str | None
    success: bool
    reason: str | None
    method: str | None
    target_id: str | None


class AuditLogsPage(BaseModel):
    items: list[AuditLogOut]
    page: int
    page_size: int
    total: int


# --- status do sistema ---------------------------------------------------------------


class ApiStatus(BaseModel):
    version: str
    env: str
    started_at: datetime
    uptime_s: int
    server_time: datetime


class CollectionStatus(BaseModel):
    name: str
    count: int | None
    size_bytes: int | None


class DatabaseStatus(BaseModel):
    ok: bool
    latency_ms: float | None
    collections: list[CollectionStatus]


class RedisStatus(BaseModel):
    configured: bool
    ok: bool


class IntegrationsStatus(BaseModel):
    google_maps: bool
    google_login: bool
    email_provider: str
    email_configured: bool
    geocoder: str


class SystemStatusOut(BaseModel):
    api: ApiStatus
    database: DatabaseStatus
    redis: RedisStatus
    integrations: IntegrationsStatus


# --- ações sobre contas --------------------------------------------------------------


class DeactivateIn(StrictModel):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] | None = None


# --- pontos de coleta (admin) ------------------------------------------------------------


class AdminPointAddressIn(StrictModel):
    street: Text120
    number: Text20 | None = None
    bairro: Text60
    city: Text60 = "Maricá"
    state: StateCode = "RJ"
    cep: Cep | None = None


def _inside_area(value: LatLng | None) -> LatLng | None:
    if value is not None and not within_area(value.lat, value.lng):
        raise ValueError("A localização está fora da área atendida (Maricá).")
    return value


def _no_duplicate_oils(value: list[OilType] | None) -> list[OilType] | None:
    if value is not None and len(set(value)) != len(value):
        raise ValueError("Tipos de óleo repetidos.")
    return value


class AdminPointIn(StrictModel):
    """Ponto criado pelo admin: mesmas regras do seed (tamanhos, CEP, UF, área de Maricá)."""

    name: PointName
    address: AdminPointAddressIn
    location: LatLng
    phone: Text30 | None = None
    opening_hours: Text200 | None = None
    accepted_oils: Annotated[list[OilType], Field(min_length=1)]
    conditions: Text300 | None = None
    verified: bool = False
    active: bool = True

    @field_validator("location")
    @classmethod
    def _check_area(cls, value: LatLng | None) -> LatLng | None:
        return _inside_area(value)

    @field_validator("accepted_oils")
    @classmethod
    def _check_oils(cls, value: list[OilType] | None) -> list[OilType] | None:
        return _no_duplicate_oils(value)


# Campos que não aceitam null num PATCH (os demais, como telefone, podem ser limpos).
REQUIRED_ON_PATCH = ("name", "address", "location", "accepted_oils", "verified", "active")


class AdminPointPatch(StrictModel):
    """Qualquer subconjunto dos campos de AdminPointIn; address substitui o endereço todo."""

    name: PointName | None = None
    address: AdminPointAddressIn | None = None
    location: LatLng | None = None
    phone: Text30 | None = None
    opening_hours: Text200 | None = None
    accepted_oils: Annotated[list[OilType], Field(min_length=1)] | None = None
    conditions: Text300 | None = None
    verified: bool | None = None
    active: bool | None = None

    @field_validator("location")
    @classmethod
    def _check_area(cls, value: LatLng | None) -> LatLng | None:
        return _inside_area(value)

    @field_validator("accepted_oils")
    @classmethod
    def _check_oils(cls, value: list[OilType] | None) -> list[OilType] | None:
        return _no_duplicate_oils(value)

    @model_validator(mode="after")
    def _no_null_for_required(self) -> Self:
        sent = self.model_fields_set
        empty = [f for f in REQUIRED_ON_PATCH if f in sent and getattr(self, f) is None]
        if empty:
            raise ValueError(f"Campo obrigatório não pode ser vazio: {', '.join(empty)}.")
        return self


class AdminPointOut(BaseModel):
    id: str
    slug: str
    name: str
    address: AddressOut
    location: LocationOut
    phone: str | None
    opening_hours: str | None
    accepted_oils: list[str]
    conditions: str | None
    verified: bool
    verified_at: datetime | date | None
    google_place_id: str | None
    active: bool
    source: str | None
    managed_by: str | None
    created_at: datetime | None
    updated_at: datetime | None


class AdminPointsPage(BaseModel):
    items: list[AdminPointOut]
    page: int
    page_size: int
    total: int


# --- alertas ------------------------------------------------------------------------------


class LoginAlert(BaseModel):
    kind: Literal["ip", "email"]
    key: str
    failures: int
    first_at: datetime
    last_at: datetime


class AlertsOut(BaseModel):
    window_min: int
    threshold: int
    generated_at: datetime
    items: list[LoginAlert]

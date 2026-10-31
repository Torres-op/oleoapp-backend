from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

ImpactPeriod = Literal["30d", "6m", "12m", "all"]
ReminderStatus = Literal["none", "ok", "soon", "overdue"]

VOLUME_MIN_ML = 1
VOLUME_MAX_ML = 10_000


class DisposalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    volume_ml: int
    point_id: str | None = None

    @field_validator("volume_ml")
    @classmethod
    def _volume_in_range(cls, value: int) -> int:
        if not VOLUME_MIN_ML <= value <= VOLUME_MAX_ML:
            raise ValueError("Informe de 1 mL a 10 L por registro.")
        return value


class DisposalPointOut(BaseModel):
    id: str
    name: str


class DisposalImpactOut(BaseModel):
    """Impacto deste descarte, com os mesmos fatores do /disposals/impact."""

    water_l: float
    co2e_kg: float


class DisposalOut(BaseModel):
    id: str
    volume_ml: int
    point: DisposalPointOut | None
    created_at: datetime
    impact: DisposalImpactOut


class DisposalsListOut(BaseModel):
    items: list[DisposalOut]


class ImpactFactorsOut(BaseModel):
    water_l_per_l_oluc: int
    co2e_kg_per_l_oluc: float


class ImpactSourcesOut(BaseModel):
    water_l_per_l_oluc: str
    co2e_kg_per_l_oluc: str


class ImpactOut(BaseModel):
    period: ImpactPeriod
    total_ml: int
    count: int
    water_l: int
    co2e_kg: float
    factors_version: str
    factors: ImpactFactorsOut
    sources: ImpactSourcesOut


class ReminderOut(BaseModel):
    status: ReminderStatus
    last_disposal_at: datetime | None
    days_since: int | None
    days_left: int | None
    reminder_interval_days: int

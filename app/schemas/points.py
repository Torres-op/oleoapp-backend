from datetime import date, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.core.geo import within_area
from app.schemas.common import LatLng


class OilType(StrEnum):
    MOTOR_CARRO = "motor_carro"  # Óleo de motor de carro
    MOTOR_MOTO = "motor_moto"  # Óleo de motor de moto
    MOTOR_DIESEL = "motor_diesel"  # Óleo de motor diesel
    OUTROS_LUBRIFICANTES = "outros_lubrificantes"  # Outros lubrificantes


Text20 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20)]
Text30 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=30)]
Text60 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=60)]
Text120 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=120)]
Text200 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Text300 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Slug = Annotated[str, StringConstraints(pattern=r"^[a-z0-9-]{3,80}$")]
PointName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]
StateCode = Annotated[
    str, StringConstraints(strip_whitespace=True, to_upper=True, pattern=r"^[A-Za-z]{2}$")
]
Cep = Annotated[str, StringConstraints(strip_whitespace=True, pattern=r"^\d{5}-\d{3}$")]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AddressSeed(StrictModel):
    street: Text120
    number: Text20 | None = None
    bairro: Text60
    city: Text60
    state: StateCode
    cep: Cep | None = None


class PointSeed(StrictModel):
    """Formato de cada item de seed/collection_points.json."""

    slug: Slug
    name: PointName
    address: AddressSeed
    location: LatLng
    phone: Text30 | None = None
    opening_hours: Text200 | None = None
    accepted_oils: Annotated[list[OilType], Field(min_length=1)]
    conditions: Text300 | None = None
    verified: bool
    verified_at: date | None = None
    source: Text60
    google_place_id: Text200 | None = None
    active: bool = True

    @field_validator("location")
    @classmethod
    def _inside_area(cls, value: LatLng) -> LatLng:
        if not within_area(value.lat, value.lng):
            raise ValueError("A localização está fora da área atendida (Maricá).")
        return value

    @field_validator("accepted_oils")
    @classmethod
    def _no_duplicate_oils(cls, value: list[OilType]) -> list[OilType]:
        if len(set(value)) != len(value):
            raise ValueError("Tipos de óleo repetidos.")
        return value


def format_address(
    street: str | None,
    number: str | None,
    bairro: str | None,
    city: str | None,
    state: str | None,
    cep: str | None,
) -> str:
    """'rua, número - bairro, cidade - UF, CEP', omitindo as partes vazias."""
    street_number = ", ".join(part for part in (street, number) if part)
    head = " - ".join(part for part in (street_number, bairro) if part)
    city_state = " - ".join(part for part in (city, state) if part)
    return ", ".join(part for part in (head, city_state, cep) if part)


class AddressOut(BaseModel):
    """Endereço de saída. Não herda as validações de entrada do seed (extra="forbid",
    formato do CEP, campos obrigatórios): um documento fora do padrão no banco não pode
    derrubar a resposta. Partes ausentes viram texto vazio (ou null em number e cep)."""

    street: str = ""
    number: str | None = None
    bairro: str = ""
    city: str = ""
    state: str = ""
    cep: str | None = None
    formatted: str = ""


class LocationOut(BaseModel):
    lat: float
    lng: float


class PointOut(BaseModel):
    id: str
    slug: str
    name: str
    address: AddressOut
    location: LocationOut
    phone: str | None
    opening_hours: str | None
    accepted_oils: list[OilType]
    conditions: str | None
    verified: bool
    verified_at: datetime | None
    google_place_id: str | None
    updated_at: datetime


class PointSummaryOut(BaseModel):
    id: str
    name: str
    bairro: str
    address_formatted: str
    location: LocationOut
    accepted_oils: list[OilType]
    verified: bool
    distance_m: int


class PointsNearOut(BaseModel):
    items: list[PointSummaryOut]

from typing import Literal

from pydantic import BaseModel


class GeocodeResult(BaseModel):
    label: str
    lat: float
    lng: float


class GeocodeOut(BaseModel):
    items: list[GeocodeResult]


class AddressSuggestion(GeocodeResult):
    """Sugestão de endereço já separada no padrão brasileiro (partes ausentes = null).

    kind: "address" (com número), "street" (rua sem número) ou "area" (bairro ou cidade).
    """

    kind: Literal["address", "street", "area"]
    street: str | None = None
    number: str | None = None
    bairro: str | None = None
    city: str | None = None
    state: str | None = None
    cep: str | None = None


class AddressSuggestOut(BaseModel):
    items: list[AddressSuggestion]

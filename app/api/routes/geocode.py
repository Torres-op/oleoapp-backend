from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.exceptions import RequestValidationError

from app.api.deps import CurrentUserDep, HttpClient, RedisDep, check_rate_limit
from app.schemas.geocode import AddressSuggestOut, GeocodeOut
from app.services import geocode as geocode_service

router = APIRouter(prefix="/geocode", tags=["geocode"])

Q_MIN_LENGTH = 3
Q_MAX_LENGTH = 120

GeocodeQuery = Annotated[str, Query(min_length=Q_MIN_LENGTH, max_length=Q_MAX_LENGTH)]


def _normalized_query(q: str) -> str:
    """Normaliza q e aplica o mínimo de novo: " a " tem 3 caracteres, mas busca só "a"."""
    normalized = geocode_service.normalize_query(q)
    if len(normalized) < Q_MIN_LENGTH:
        raise RequestValidationError(
            [
                {
                    "type": "string_too_short",
                    "loc": ("query", "q"),
                    "msg": f"String should have at least {Q_MIN_LENGTH} characters",
                    "input": q,
                    "ctx": {"min_length": Q_MIN_LENGTH},
                }
            ]
        )
    return normalized


@router.get("", response_model=GeocodeOut)
async def geocode(
    q: GeocodeQuery,
    http: HttpClient,
    redis: RedisDep,
    user: CurrentUserDep,
) -> GeocodeOut:
    normalized = _normalized_query(q)
    # O limite é contado aqui, depois da validação: um 422 não gasta a cota do usuário.
    await check_rate_limit(redis, "geocode", "RATE_LIMIT_GEOCODE_PER_MIN", 60, user.id)
    items = await geocode_service.search(redis, http, normalized)
    return GeocodeOut(items=items)


@router.get("/suggest", response_model=AddressSuggestOut)
async def suggest(
    q: GeocodeQuery,
    http: HttpClient,
    redis: RedisDep,
    user: CurrentUserDep,
) -> AddressSuggestOut:
    """Sugestões enquanto o usuário digita (rua, número, bairro, cidade, UF, CEP e lat/lng)."""
    normalized = _normalized_query(q)
    await check_rate_limit(
        redis, "geocode_suggest", "RATE_LIMIT_GEOCODE_SUGGEST_PER_MIN", 60, user.id
    )
    items = await geocode_service.suggest(redis, http, normalized)
    return AddressSuggestOut(items=items)

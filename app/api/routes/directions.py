from fastapi import APIRouter

from app.api.deps import CurrentUserDep, Db, HttpClient, RedisDep, check_rate_limit
from app.schemas.directions import DirectionsIn, DirectionsOut
from app.services import directions as directions_service

router = APIRouter(prefix="/directions", tags=["directions"])


@router.post("", response_model=DirectionsOut)
async def get_directions(
    payload: DirectionsIn, db: Db, http: HttpClient, redis: RedisDep, user: CurrentUserDep
) -> DirectionsOut:
    # O limite é contado aqui, depois da validação: um 422 não gasta a cota do usuário.
    await check_rate_limit(redis, "directions", "RATE_LIMIT_DIRECTIONS_PER_HOUR", 3600, user.id)
    return await directions_service.get_directions(db, http, payload)

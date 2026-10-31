import logging

from fastapi import APIRouter

from app.api.deps import Db, RedisDep
from app.core.errors import AppError

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    # Não acessa banco nem Redis: é o health check do Render.
    return {"status": "ok"}


@router.get("/health/ready")
async def ready(db: Db, redis: RedisDep) -> dict[str, str]:
    try:
        await db.command("ping")
    except Exception as exc:
        logger.warning("Ping no Mongo falhou: %s", type(exc).__name__)
        raise AppError(503, "service_unavailable", "Serviço indisponível.") from exc

    redis_state = "disabled"
    if redis is not None:
        try:
            await redis.ping()
            redis_state = "ok"
        except Exception:
            redis_state = "degraded"
    return {"status": "ok", "mongo": "ok", "redis": redis_state}

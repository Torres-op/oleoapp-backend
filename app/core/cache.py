import json
import logging
from typing import Any

from redis.asyncio import Redis

logger = logging.getLogger(__name__)


async def get_json(redis: Redis | None, key: str) -> Any | None:
    if redis is None:
        return None
    try:
        raw = await redis.get(key)
        return None if raw is None else json.loads(raw)
    except Exception:
        logger.warning("Redis indisponível no cache (leitura); ignorando.")
        return None


async def set_json(redis: Redis | None, key: str, value: Any, ttl_s: int) -> None:
    if redis is None:
        return
    try:
        await redis.set(key, json.dumps(value), ex=ttl_s)
    except Exception:
        logger.warning("Redis indisponível no cache (escrita); ignorando.")

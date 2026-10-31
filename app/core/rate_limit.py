import logging

from fastapi import Request
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from app.core.config import settings
from app.core.errors import AppError, error_response

logger = logging.getLogger(__name__)

RATE_LIMITED_CODE = "rate_limited"
RATE_LIMITED_MESSAGE = "Muitas requisições. Tente de novo em instantes."


async def hit(redis: Redis | None, key: str, limit: int, window_s: int) -> tuple[bool, int]:
    """Janela fixa no Redis. Devolve (permitido, segundos_para_liberar).

    Sem Redis, ou com qualquer erro dele, permite a requisição (falha aberta).
    """
    if redis is None:
        return True, 0
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window_s)
        ttl = await redis.ttl(key)
        if ttl < 0:
            # Chave sem expiração (falha entre INCR e EXPIRE): corrige para não travar.
            await redis.expire(key, window_s)
            ttl = window_s
    except Exception:
        logger.warning("Redis indisponível no rate limit; requisição liberada.")
        return True, 0
    if count > limit:
        return False, max(int(ttl), 1)
    return True, 0


def rate_limited_error(retry_after: int) -> AppError:
    """O erro 429 padrão da API, com o Retry-After."""
    return AppError(
        429,
        RATE_LIMITED_CODE,
        RATE_LIMITED_MESSAGE,
        headers={"Retry-After": str(retry_after)},
    )


def rate_limited_response(retry_after: int) -> JSONResponse:
    """Mesmo 429 de rate_limited_error, para quem responde direto (middlewares)."""
    error = rate_limited_error(retry_after)
    return error_response(error.status, error.code, error.message, headers=error.headers)


async def enforce(redis: Redis | None, key: str, limit: int, window_s: int) -> None:
    """Conta a requisição e levanta o 429 padrão se o limite estourou."""
    allowed, retry_after = await hit(redis, key, limit, window_s)
    if not allowed:
        raise rate_limited_error(retry_after)


def client_ip(request: Request) -> str:
    """IP do cliente para rate limit.

    Cada proxy confiável acrescenta, à direita do X-Forwarded-For, o IP de quem se
    conectou a ele. Por isso só a entrada na posição -TRUSTED_PROXY_HOPS é confiável:
    o que estiver mais à esquerda pode ter sido enviado pelo próprio cliente.
    Com TRUSTED_PROXY_HOPS=0 (sem proxy), usa o IP da conexão.
    """
    hops = settings.TRUSTED_PROXY_HOPS
    forwarded = request.headers.get("x-forwarded-for")
    if hops > 0 and forwarded:
        parts = [part.strip() for part in forwarded.split(",") if part.strip()]
        if len(parts) >= hops:
            return parts[-hops]
    return request.client.host if request.client else "unknown"

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI

from app.api.router import api_router
from app.core.background import drain as drain_background_tasks
from app.core.config import APP_VERSION, settings
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import register_middlewares
from app.db.indexes import ensure_indexes
from app.db.mongo import create_mongo_client, get_database

logger = logging.getLogger(__name__)

BACKGROUND_DRAIN_TIMEOUT_S = 15


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    app.state.started_at = datetime.now(UTC)  # uptime no status do painel de administração
    client = create_mongo_client()  # um único cliente para toda a aplicação
    app.state.db = get_database(client)

    try:
        await ensure_indexes(app.state.db)
    except Exception:
        # Atlas inacessível: a API sobe mesmo assim; os índices são criados no próximo início.
        logger.exception("Falha ao criar índices; a API continua subindo.")

    redis_client = None
    if settings.REDIS_URL:
        # Timeouts curtos: com o Redis fora do ar, o rate limit falha aberto em vez de travar.
        redis_client = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    else:
        logger.warning("Redis desativado (REDIS_URL vazio): sem rate limit e sem cache.")
    app.state.redis = redis_client

    app.state.http = httpx.AsyncClient(timeout=10)

    try:
        yield
    finally:
        # E-mails ainda em envio terminam antes de o cliente HTTP e o banco fecharem.
        await drain_background_tasks(timeout=BACKGROUND_DRAIN_TIMEOUT_S)
        await app.state.http.aclose()
        if redis_client is not None:
            await redis_client.aclose()
        await client.close()


def create_app() -> FastAPI:
    configure_logging()
    is_production = settings.ENV == "production"
    app = FastAPI(
        title="ÓleoApp API",
        version=APP_VERSION,
        lifespan=lifespan,
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )
    register_middlewares(app)
    register_error_handlers(app)
    app.include_router(api_router)  # sem CORS: o frontend usa rewrite na mesma origem
    return app


app = create_app()

from typing import Any

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import settings


def create_mongo_client() -> AsyncMongoClient[dict[str, Any]]:
    """Cria o cliente único da aplicação (conexão preguiçosa, pool pequeno para o Atlas M0)."""
    return AsyncMongoClient(
        settings.MONGO_URL,
        tz_aware=True,
        maxPoolSize=settings.MONGO_MAX_POOL_SIZE,
        minPoolSize=0,
        serverSelectionTimeoutMS=settings.MONGO_TIMEOUT_MS,
        connectTimeoutMS=settings.MONGO_TIMEOUT_MS,
        appname="oleoapp-api",
    )


def get_database(client: AsyncMongoClient[dict[str, Any]]) -> AsyncDatabase[dict[str, Any]]:
    return client[settings.MONGO_DB_NAME]

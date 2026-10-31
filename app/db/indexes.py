import logging
from typing import Any

from pymongo import ASCENDING, DESCENDING, GEOSPHERE
from pymongo.asynchronous.database import AsyncDatabase

from app.core.config import settings
from app.db.collections import (
    AUDIT_LOGS,
    DISPOSALS,
    FAVORITES,
    OAUTH_PENDING_LINKS,
    OAUTH_STATES,
    PASSWORD_RESETS,
    POINTS,
    SESSIONS,
    USERS,
)

logger = logging.getLogger(__name__)


async def _ensure_audit_ttl(db: AsyncDatabase[dict[str, Any]]) -> None:
    """Índice TTL de created_at na auditoria (também serve para ordenar e filtrar por data).

    Se a retenção mudar (AUDIT_LOG_RETENTION_DAYS), ajusta o índice existente com collMod.
    """
    ttl = settings.AUDIT_LOG_RETENTION_DAYS * 86400
    current = (await db[AUDIT_LOGS].index_information()).get("created_at_1")
    if current is None:
        await db[AUDIT_LOGS].create_index([("created_at", ASCENDING)], expireAfterSeconds=ttl)
    elif current.get("expireAfterSeconds") != ttl:
        try:
            await db.command(
                "collMod",
                AUDIT_LOGS,
                index={"keyPattern": {"created_at": 1}, "expireAfterSeconds": ttl},
            )
        except Exception as exc:
            # O usuário do banco pode não ter permissão de collMod (só readWrite).
            logger.warning("Não foi possível ajustar a retenção da auditoria: %s", exc)


async def ensure_indexes(db: AsyncDatabase[dict[str, Any]]) -> None:
    """Cria (se faltarem) todos os índices da seção 6. É idempotente."""
    await db[USERS].create_index([("email", ASCENDING)], unique=True)
    # Painel de administração: lista por data de cadastro e usuários ativos.
    await db[USERS].create_index([("created_at", DESCENDING)])
    await db[USERS].create_index([("last_login_at", DESCENDING)])
    # Login social: uma conta do provedor só pode estar ligada a um usuário.
    for provider in ("google",):
        field = f"oauth.{provider}"
        await db[USERS].create_index(
            [(field, ASCENDING)],
            unique=True,
            partialFilterExpression={field: {"$exists": True}},
        )

    # Estados do OAuth e vínculos pendentes vivem 10 minutos (expires_at definido na criação).
    await db[OAUTH_STATES].create_index([("state_hash", ASCENDING)], unique=True)
    await db[OAUTH_STATES].create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    await db[OAUTH_PENDING_LINKS].create_index([("token_hash", ASCENDING)], unique=True)
    await db[OAUTH_PENDING_LINKS].create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    await db[OAUTH_PENDING_LINKS].create_index([("user_id", ASCENDING)])

    await db[SESSIONS].create_index([("token_hash", ASCENDING)], unique=True)
    await db[SESSIONS].create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    await db[SESSIONS].create_index([("user_id", ASCENDING)])

    await db[PASSWORD_RESETS].create_index([("token_hash", ASCENDING)], unique=True)
    await db[PASSWORD_RESETS].create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
    await db[PASSWORD_RESETS].create_index([("user_id", ASCENDING)])

    await db[POINTS].create_index([("location", GEOSPHERE)])
    await db[POINTS].create_index([("slug", ASCENDING)], unique=True)
    await db[POINTS].create_index([("active", ASCENDING)])

    await db[DISPOSALS].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])
    await db[DISPOSALS].create_index([("created_at", DESCENDING)])

    await _ensure_audit_ttl(db)
    await db[AUDIT_LOGS].create_index([("type", ASCENDING), ("created_at", DESCENDING)])
    await db[AUDIT_LOGS].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])

    await db[FAVORITES].create_index([("user_id", ASCENDING), ("point_id", ASCENDING)], unique=True)
    await db[FAVORITES].create_index([("user_id", ASCENDING), ("created_at", DESCENDING)])

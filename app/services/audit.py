"""Log de auditoria (coleção audit_logs).

Grava quem fez o quê, quando e de onde: login (sucesso e falha), logout, cadastro, exclusão
de conta, descarte e ações de administração. Nunca grava senha, token, hash nem cookie.
Falhar ao gravar não pode derrubar o fluxo principal: o erro vira um aviso no log.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase

from app.repositories import audit as audit_repo
from app.services.email import mask_email

logger = logging.getLogger(__name__)

EventType = Literal[
    "login_success",
    "login_failure",
    "logout",
    "signup",
    "account_deleted",
    "disposal_created",
    "admin_promoted",
    "admin_revoked",
    "admin_access",
    "admin_user_deactivated",
    "admin_user_reactivated",
    "admin_point_created",
    "admin_point_updated",
]
EVENT_TYPES: tuple[str, ...] = EventType.__args__
Method = Literal["password", "google"]

USER_AGENT_MAX = 200
REASON_MAX = 200


@dataclass(frozen=True)
class RequestContext:
    """Origem da ação (IP e navegador), passada pelas rotas para a auditoria."""

    ip: str | None = None
    user_agent: str | None = None


def _object_id(user_id: str | ObjectId | None) -> ObjectId | None:
    if user_id is None or isinstance(user_id, ObjectId):
        return user_id
    return ObjectId(user_id)


async def record(
    db: AsyncDatabase,
    event_type: EventType,
    context: RequestContext,
    *,
    user_id: str | ObjectId | None = None,
    email: str | None = None,
    success: bool = True,
    reason: str | None = None,
    method: Method | None = None,
    target_id: str | ObjectId | None = None,
) -> None:
    """Grava um evento. Nas falhas de login, passe o e-mail digitado: ele é mascarado.

    Nas ações de administração, user_id é o admin e target_id é o usuário ou ponto alvo.
    """
    if event_type == "login_failure" and email:
        email = mask_email(email)
    try:
        await audit_repo.insert(
            db,
            {
                "created_at": datetime.now(UTC),
                "type": event_type,
                "user_id": _object_id(user_id),
                "email": email,
                "ip": context.ip,
                "user_agent": (context.user_agent or "")[:USER_AGENT_MAX] or None,
                "success": success,
                "reason": reason[:REASON_MAX] if reason else None,
                "method": method,
                "target_id": str(target_id) if target_id is not None else None,
            },
        )
    except Exception as exc:
        # Só o tipo do evento e do erro: nada de e-mail, IP ou ids no log.
        logger.warning("Falha ao gravar auditoria (%s): %s", event_type, type(exc).__name__)


async def anonymize_user(db: AsyncDatabase, user_id: str | ObjectId) -> None:
    """Na exclusão da conta: mantém os eventos, sem id nem e-mail da pessoa."""
    try:
        await audit_repo.anonymize_user(db, _object_id(user_id))
    except Exception as exc:
        logger.warning("Falha ao anonimizar a auditoria: %s", type(exc).__name__)

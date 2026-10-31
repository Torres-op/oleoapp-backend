from dataclasses import dataclass
from typing import Annotated, Literal

import httpx
from fastapi import Depends, Request
from pymongo.asynchronous.database import AsyncDatabase
from redis.asyncio import Redis

from app.core.config import settings
from app.core.errors import AppError
from app.core.rate_limit import client_ip, enforce
from app.core.security import hash_token
from app.repositories import sessions, users
from app.services.audit import RequestContext


async def get_db(request: Request) -> AsyncDatabase:
    return request.app.state.db


async def get_redis(request: Request) -> Redis | None:
    return request.app.state.redis


async def get_http_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.http


Db = Annotated[AsyncDatabase, Depends(get_db)]
RedisDep = Annotated[Redis | None, Depends(get_redis)]
HttpClient = Annotated[httpx.AsyncClient, Depends(get_http_client)]


Role = Literal["user", "admin"]


def user_role(document: dict) -> Role:
    """Papel do usuário; sem o campo (contas antigas) vale "user"."""
    return "admin" if document.get("role") == "admin" else "user"


@dataclass(frozen=True)
class CurrentUser:
    id: str
    name: str
    email: str
    reminder_interval_days: int
    role: Role = "user"


async def get_current_user(request: Request, db: Db) -> CurrentUser:
    def unauthorized() -> AppError:
        return AppError(401, "unauthorized", "Sessão expirada. Entre novamente.")

    token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    if not token:
        raise unauthorized()
    session = await sessions.find_valid(db, hash_token(token))
    if session is None:
        raise unauthorized()
    user = await users.find_by_id(db, session["user_id"])
    # Conta desativada pelo admin perde as sessões; isto é uma defesa a mais.
    if user is None or user.get("disabled_at"):
        raise unauthorized()
    return CurrentUser(
        id=str(user["_id"]),
        name=user["name"],
        email=user["email"],
        reminder_interval_days=user["reminder_interval_days"],
        role=user_role(user),
    )


CurrentUserDep = Annotated[CurrentUser, Depends(get_current_user)]


async def get_request_context(request: Request) -> RequestContext:
    return RequestContext(ip=client_ip(request), user_agent=request.headers.get("user-agent"))


RequestCtx = Annotated[RequestContext, Depends(get_request_context)]


async def require_admin(user: CurrentUserDep) -> CurrentUser:
    """Só administradores. Sem login: 401 (de get_current_user). Sem permissão: 403.

    O papel vem do banco a cada requisição; nada do que o frontend envia é considerado.
    """
    if user.role != "admin":
        raise AppError(403, "forbidden", "Acesso restrito à administração.")
    return user


AdminUserDep = Annotated[CurrentUser, Depends(require_admin)]


async def check_rate_limit(
    redis: Redis | None, scope: str, limit_setting: str, window_s: int, identifier: str
) -> None:
    """Conta a requisição no limite `scope` e levanta 429 se estourou.

    O limite é lido de `settings` a cada chamada. Rotas que gastam cota de API externa
    chamam isto dentro do handler, depois da validação: requisição com 422 não conta.
    """
    limit = getattr(settings, limit_setting)
    await enforce(redis, f"rl:{scope}:{identifier}", limit, window_s)


def rate_limit(scope: str, limit_setting: str, window_s: int, by: Literal["ip", "user"]):
    """Dependência de rate limit. O limite é lido de `settings` a cada requisição."""

    if by == "user":

        async def by_user(redis: RedisDep, user: CurrentUserDep) -> None:
            await check_rate_limit(redis, scope, limit_setting, window_s, user.id)

        return by_user

    async def by_ip(request: Request, redis: RedisDep) -> None:
        await check_rate_limit(redis, scope, limit_setting, window_s, client_ip(request))

    return by_ip


async def admin_rate_limit(redis: RedisDep, admin: AdminUserDep) -> None:
    """Exige admin e conta a requisição no limite por administrador."""
    await check_rate_limit(redis, "admin", "RATE_LIMIT_ADMIN_PER_MIN", 60, admin.id)

"""Login social com Google (OAuth 2.0, fluxo authorization code, com PKCE).

O navegador vai ao provedor por /api/auth/oauth/<provedor>/start e volta em .../callback,
que sempre responde com um redirecionamento para o frontend. O state (anti-CSRF) fica no
Mongo (só o hash) e num cookie HttpOnly.

Importante: nunca logar code, tokens, e-mail nem secret (só o provedor e o status HTTP).
"""

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
from bson import ObjectId
from pydantic import TypeAdapter, ValidationError
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.security import hash_token
from app.repositories import oauth as oauth_repo
from app.repositories import users
from app.schemas.auth import Email, UserOut
from app.services.auth import OAUTH_PROVIDERS, mark_login, open_session, to_user_out

logger = logging.getLogger(__name__)

STATE_COOKIE = "oleo_oauth_state"
STATE_COOKIE_PATH = "/api/auth/oauth"
LINK_COOKIE = "oleo_oauth_link"
LINK_COOKIE_PATH = "/api/auth"
TTL = timedelta(minutes=10)
DEFAULT_NEXT = "/map"
# Código de /login?erro=<código> para conta desativada pelo admin.
ACCOUNT_DISABLED = "conta_desativada"

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

NAME_MIN, NAME_MAX = 2, 80
_email_adapter: TypeAdapter[str] = TypeAdapter(Email)


class OAuthFailure(Exception):
    """Erro do fluxo, com o código que o frontend recebe em /login?erro=<código>."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Profile:
    provider_user_id: str
    email: str | None
    email_verified: bool
    name: str


@dataclass
class Outcome:
    """Resultado do callback: sessão aberta (session_token) ou erro (error)."""

    next_path: str = DEFAULT_NEXT
    session_token: str | None = None
    error: str | None = None
    link_token: str | None = None
    # Para a auditoria: quem entrou e se a conta foi criada agora.
    user_id: ObjectId | None = None
    email: str | None = None
    created: bool = False


def _now() -> datetime:
    return datetime.now(UTC)


def _credentials(provider: str) -> tuple[str, str]:
    if provider == "google":
        return settings.GOOGLE_OAUTH_CLIENT_ID, settings.GOOGLE_OAUTH_CLIENT_SECRET
    return "", ""


def is_configured(provider: str) -> bool:
    client_id, secret = _credentials(provider)
    return provider in OAUTH_PROVIDERS and bool(client_id) and bool(secret)


def providers_status() -> dict[str, bool]:
    return {provider: is_configured(provider) for provider in OAUTH_PROVIDERS}


def frontend_url(path: str) -> str:
    return settings.FRONTEND_URL.rstrip("/") + path


def redirect_uri(provider: str) -> str:
    return frontend_url(f"/api/auth/oauth/{provider}/callback")


def safe_next(value: str | None) -> str:
    """Só caminhos do próprio site: começa com "/" e não com "//" nem "/\\"."""
    if not value or not value.startswith("/") or value.startswith(("//", "/\\")):
        return DEFAULT_NEXT
    return value


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


async def start(db: AsyncDatabase, provider: str, next_path: str | None) -> tuple[str, str]:
    """Registra o state e devolve (URL de autorização do provedor, state para o cookie)."""
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    now = _now()
    await oauth_repo.create_state(
        db,
        {
            "state_hash": hash_token(state),
            "provider": provider,
            "next": safe_next(next_path),
            "code_verifier": verifier,
            "created_at": now,
            "expires_at": now + TTL,
        },
    )

    client_id, _ = _credentials(provider)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri(provider),
        "response_type": "code",
        "state": state,
        "scope": "openid email profile",
        "prompt": "select_account",
        "code_challenge": _pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    return f"{GOOGLE_AUTHORIZE_URL}?{urlencode(params)}", state


async def _json(response_coro: Any, provider: str, step: str) -> dict[str, Any]:
    try:
        response = await response_coro
    except httpx.HTTPError as exc:
        logger.warning("OAuth %s (%s): falha de rede %s", provider, step, type(exc).__name__)
        raise OAuthFailure("oauth_falhou") from None
    if response.status_code != 200:
        logger.warning("OAuth %s (%s): HTTP %s", provider, step, response.status_code)
        raise OAuthFailure("oauth_falhou")
    try:
        data = response.json()
    except ValueError:
        logger.warning("OAuth %s (%s): resposta não é JSON", provider, step)
        raise OAuthFailure("oauth_falhou") from None
    if not isinstance(data, dict):
        raise OAuthFailure("oauth_falhou")
    return data


def _token_from(data: dict[str, Any]) -> str:
    token = data.get("access_token")
    if not isinstance(token, str) or not token:
        raise OAuthFailure("oauth_falhou")
    return token


async def _google_profile(http: httpx.AsyncClient, code: str, verifier: str) -> Profile:
    client_id, secret = _credentials("google")
    token_data = await _json(
        http.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": secret,
                "redirect_uri": redirect_uri("google"),
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
        ),
        "google",
        "token",
    )
    access_token = _token_from(token_data)
    info = await _json(
        http.get(GOOGLE_USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}),
        "google",
        "perfil",
    )
    sub = info.get("sub")
    if not isinstance(sub, str) or not sub:
        raise OAuthFailure("oauth_falhou")
    return Profile(
        provider_user_id=sub,
        email=info.get("email") if isinstance(info.get("email"), str) else None,
        email_verified=info.get("email_verified") is True,
        name=info.get("name") if isinstance(info.get("name"), str) else "",
    )


def _normalized_email(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        return _email_adapter.validate_python(raw)
    except ValidationError:
        return None


def display_name(raw: str, email: str) -> str:
    """Nome do provedor limpo e no limite do cadastro; senão, o começo do e-mail."""
    for candidate in (raw, email.split("@", 1)[0]):
        cleaned = " ".join(candidate.split())[:NAME_MAX].strip()
        if len(cleaned) >= NAME_MIN:
            return cleaned
    return "Usuário"


async def _signed_in(
    db: AsyncDatabase, user_id: ObjectId, email: str, *, created: bool = False
) -> Outcome:
    token = await open_session(db, user_id)
    await mark_login(db, user_id)
    return Outcome(session_token=token, user_id=user_id, email=email, created=created)


def _disabled(document: dict[str, Any]) -> Outcome | None:
    """Conta desativada pelo admin: não entra (nem vincula) pelo login social."""
    if document.get("disabled_at"):
        return Outcome(error=ACCOUNT_DISABLED, user_id=document["_id"], email=document["email"])
    return None


async def _resolve_user(db: AsyncDatabase, provider: str, profile: Profile) -> Outcome:
    linked = await users.find_by_oauth(db, provider, profile.provider_user_id)
    if linked is not None:
        return _disabled(linked) or await _signed_in(db, linked["_id"], linked["email"])

    email = _normalized_email(profile.email)
    if email is None:
        raise OAuthFailure("oauth_sem_email")

    existing = await users.find_by_email(db, email)
    if existing is not None:
        if blocked := _disabled(existing):
            return blocked
        if (existing.get("oauth") or {}).get(provider):
            # A conta já está ligada a outra conta deste provedor.
            raise OAuthFailure("oauth_falhou")
        if profile.email_verified:
            await users.update_fields(
                db,
                existing["_id"],
                {f"oauth.{provider}": profile.provider_user_id, "updated_at": _now()},
            )
            return await _signed_in(db, existing["_id"], existing["email"])
        # E-mail não confirmado pelo provedor: só vincula depois de entrar com a senha.
        link_token = secrets.token_urlsafe(32)
        now = _now()
        await oauth_repo.create_pending_link(
            db,
            {
                "token_hash": hash_token(link_token),
                "user_id": existing["_id"],
                "provider": provider,
                "provider_user_id": profile.provider_user_id,
                "created_at": now,
                "expires_at": now + TTL,
            },
        )
        return Outcome(error="oauth_vincular", link_token=link_token, email=email)

    now = _now()
    document: dict[str, Any] = {
        "name": display_name(profile.name, email),
        "email": email,
        "password_hash": None,
        "oauth": {provider: profile.provider_user_id},
        "bairro": None,
        "reminder_interval_days": 180,
        "created_at": now,
        "updated_at": now,
    }
    try:
        user_id = await users.insert(db, document)
    except DuplicateKeyError:
        raise OAuthFailure("oauth_falhou") from None
    return await _signed_in(db, user_id, email, created=True)


async def callback(
    db: AsyncDatabase,
    http: httpx.AsyncClient,
    provider: str,
    *,
    code: str | None,
    state: str | None,
    error: str | None,
    cookie_state: str | None,
) -> Outcome:
    if not is_configured(provider):
        return Outcome(error="oauth_indisponivel")
    if error:
        return Outcome(error="oauth_cancelado")
    if not state or not cookie_state or not secrets.compare_digest(state, cookie_state):
        return Outcome(error="oauth_falhou")
    record = await oauth_repo.pop_state(db, hash_token(state))
    if record is None or record.get("provider") != provider or not code:
        return Outcome(error="oauth_falhou")

    next_path = safe_next(record.get("next"))
    try:
        profile = await _google_profile(http, code, record.get("code_verifier") or "")
        outcome = await _resolve_user(db, provider, profile)
    except OAuthFailure as exc:
        return Outcome(error=exc.code)
    outcome.next_path = next_path
    return outcome


async def complete_pending_link(db: AsyncDatabase, link_token: str, user_id: str) -> UserOut | None:
    """Depois de um login com senha: vincula o provedor pendente se for deste usuário."""
    token_hash = hash_token(link_token)
    pending = await oauth_repo.find_pending_link(db, token_hash)
    if pending is None or pending["user_id"] != ObjectId(user_id):
        return None
    provider = pending["provider"]
    try:
        updated = await users.update_fields(
            db,
            pending["user_id"],
            {f"oauth.{provider}": pending["provider_user_id"], "updated_at": _now()},
        )
    except DuplicateKeyError:
        # A conta do provedor já foi ligada a outro usuário nesse meio-tempo.
        updated = None
    await oauth_repo.delete_pending_link(db, token_hash)
    return to_user_out(updated) if updated is not None else None

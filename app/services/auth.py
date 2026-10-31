import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from bson import ObjectId
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import DuplicateKeyError

from app.api.deps import user_role
from app.core.config import settings
from app.core.errors import AppError
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    hash_password,
    hash_token,
    new_session_token,
    verify_password,
)
from app.repositories import sessions, users
from app.schemas.auth import (
    ChangeEmailIn,
    ChangePasswordIn,
    DeleteAccountIn,
    LoginIn,
    SetPasswordIn,
    SignupIn,
    UpdateMeIn,
    UserOut,
)


def _now() -> datetime:
    # O Mongo guarda milissegundos; truncamos para a resposta ser igual à leitura posterior.
    now = datetime.now(UTC)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def _email_in_use() -> AppError:
    return AppError(409, "email_in_use", "Este e-mail já está cadastrado.")


def _invalid_password() -> AppError:
    return AppError(400, "invalid_password", "Senha atual incorreta.")


def _unauthorized() -> AppError:
    return AppError(401, "unauthorized", "Sessão expirada. Entre novamente.")


def _password_required(message: str) -> AppError:
    return AppError(400, "password_required", message)


# Ordem fixa dos provedores de login social na resposta.
OAUTH_PROVIDERS = ("google",)


def to_user_out(document: dict[str, Any]) -> UserOut:
    linked = document.get("oauth") or {}
    return UserOut(
        id=str(document["_id"]),
        name=document["name"],
        email=document["email"],
        bairro=document.get("bairro"),
        reminder_interval_days=document["reminder_interval_days"],
        created_at=document["created_at"],
        # Contas criadas só com o Google não têm senha (password_hash None).
        has_password=bool(document.get("password_hash")),
        providers=[provider for provider in OAUTH_PROVIDERS if linked.get(provider)],
        role=user_role(document),
    )


async def mark_login(db: AsyncDatabase, user_id: ObjectId) -> None:
    """Guarda a data do último login (usada nas métricas de usuários ativos)."""
    await users.update_fields(db, user_id, {"last_login_at": _now()})


async def open_session(db: AsyncDatabase, user_id: ObjectId) -> str:
    token, token_hash = new_session_token()
    expires_at = _now() + timedelta(days=settings.SESSION_TTL_DAYS)
    await sessions.create(db, user_id, token_hash, expires_at)
    return token


async def _check_password(password: str, password_hash: str) -> None:
    # O Argon2 é CPU-bound: roda em thread para não travar o event loop.
    if not await asyncio.to_thread(verify_password, password, password_hash):
        raise _invalid_password()


async def _load_user(db: AsyncDatabase, user_id: str) -> dict[str, Any]:
    document = await users.find_by_id(db, ObjectId(user_id))
    if document is None:
        raise _unauthorized()
    return document


async def signup(db: AsyncDatabase, data: SignupIn) -> tuple[UserOut, str]:
    if await users.find_by_email(db, data.email) is not None:
        raise _email_in_use()

    now = _now()
    document: dict[str, Any] = {
        "name": data.name,
        "email": data.email,
        "password_hash": await asyncio.to_thread(hash_password, data.password),
        "bairro": None,
        "reminder_interval_days": 180,
        "created_at": now,
        "updated_at": now,
    }
    try:
        user_id = await users.insert(db, document)
    except DuplicateKeyError as exc:
        raise _email_in_use() from exc

    token = await open_session(db, user_id)
    return to_user_out(document), token


async def login(db: AsyncDatabase, data: LoginIn) -> tuple[UserOut, str]:
    document = await users.find_by_email(db, data.email)
    # Sem usuário (ou conta só com login social, sem senha), verifica contra o hash fictício:
    # o tempo e a resposta não revelam se a conta existe nem como ela entra.
    stored_hash = document.get("password_hash") if document is not None else None
    valid = await asyncio.to_thread(
        verify_password, data.password, stored_hash or DUMMY_PASSWORD_HASH
    )
    if not valid or not stored_hash or document is None:
        raise AppError(401, "invalid_credentials", "E-mail ou senha incorretos.")
    # Só depois da senha certa: senha errada não revela que a conta está desativada.
    if document.get("disabled_at"):
        raise AppError(403, "account_disabled", "Esta conta foi desativada. Fale com o suporte.")

    token = await open_session(db, document["_id"])
    await mark_login(db, document["_id"])
    return to_user_out(document), token


async def logout(db: AsyncDatabase, token: str | None) -> ObjectId | None:
    """Apaga a sessão (se existir) e devolve o id do dono dela, para a auditoria."""
    if not token:
        return None
    token_hash = hash_token(token)
    session = await sessions.find_valid(db, token_hash)
    await sessions.delete_by_hash(db, token_hash)
    return session["user_id"] if session is not None else None


async def get_me(db: AsyncDatabase, user_id: str) -> UserOut:
    return to_user_out(await _load_user(db, user_id))


async def update_me(db: AsyncDatabase, user_id: str, data: UpdateMeIn) -> UserOut:
    fields = data.model_dump(exclude_unset=True)
    fields["updated_at"] = _now()
    document = await users.update_fields(db, ObjectId(user_id), fields)
    if document is None:
        raise _unauthorized()
    return to_user_out(document)


async def change_email(db: AsyncDatabase, user_id: str, data: ChangeEmailIn) -> UserOut:
    document = await _load_user(db, user_id)
    if not document.get("password_hash"):
        raise _password_required("Crie uma senha antes de trocar o e-mail.")
    await _check_password(data.current_password, document["password_hash"])

    existing = await users.find_by_email(db, data.new_email)
    if existing is not None and existing["_id"] != document["_id"]:
        raise _email_in_use()

    try:
        updated = await users.update_fields(
            db, document["_id"], {"email": data.new_email, "updated_at": _now()}
        )
    except DuplicateKeyError as exc:
        raise _email_in_use() from exc
    if updated is None:
        raise _unauthorized()
    return to_user_out(updated)


async def change_password(
    db: AsyncDatabase, user_id: str, data: ChangePasswordIn, current_token: str | None
) -> None:
    document = await _load_user(db, user_id)
    if not document.get("password_hash"):
        raise _password_required('Esta conta ainda não tem senha. Use "Criar senha".')
    await _check_password(data.current_password, document["password_hash"])

    new_hash = await asyncio.to_thread(hash_password, data.new_password)
    await users.update_fields(
        db, document["_id"], {"password_hash": new_hash, "updated_at": _now()}
    )
    # Derruba as outras sessões; a atual continua valendo.
    keep = hash_token(current_token) if current_token else None
    await sessions.delete_by_user(db, document["_id"], except_hash=keep)


async def set_password(db: AsyncDatabase, user_id: str, data: SetPasswordIn) -> UserOut:
    document = await _load_user(db, user_id)
    if document.get("password_hash"):
        raise AppError(409, "password_already_set", "Esta conta já tem senha.")

    new_hash = await asyncio.to_thread(hash_password, data.new_password)
    updated = await users.update_fields(
        db, document["_id"], {"password_hash": new_hash, "updated_at": _now()}
    )
    if updated is None:
        raise _unauthorized()
    return to_user_out(updated)


async def delete_account(db: AsyncDatabase, user_id: str, data: DeleteAccountIn) -> None:
    document = await _load_user(db, user_id)
    if document.get("password_hash"):
        if not data.current_password:
            raise _invalid_password()
        await _check_password(data.current_password, document["password_hash"])
    else:
        # Conta só com login social: confirma digitando o próprio e-mail.
        typed = (data.confirm_email or "").strip().lower()
        if typed != document["email"]:
            raise AppError(
                400, "confirmation_mismatch", "Digite o e-mail da sua conta para confirmar."
            )

    await sessions.delete_by_user(db, document["_id"])
    await users.delete_related(db, document["_id"])
    await users.delete(db, document["_id"])

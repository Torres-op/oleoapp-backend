import asyncio
import logging
import secrets
from datetime import UTC, datetime, timedelta
from html import escape

import httpx
from pymongo.asynchronous.database import AsyncDatabase

from app.core.background import spawn
from app.core.config import settings
from app.core.errors import AppError
from app.core.security import hash_password, hash_token
from app.repositories import password_resets, sessions, users
from app.services.email import EmailSendError, mask_email, send_email

logger = logging.getLogger(__name__)

SUBJECT = "Redefinição de senha — ÓleoApp"


def _now() -> datetime:
    # O Mongo guarda milissegundos; truncamos para o valor gravado ser igual ao lido depois.
    now = datetime.now(UTC)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def _invalid_token() -> AppError:
    return AppError(400, "invalid_token", "Link inválido ou expirado. Peça um novo.")


def _build_message(link: str, minutes: int) -> tuple[str, str]:
    """Texto e HTML simples (sem imagens e sem rastreamento)."""
    text = (
        "Olá,\n\n"
        "Recebemos um pedido para redefinir a senha da sua conta no ÓleoApp. "
        "Para criar uma nova senha, abra o link abaixo:\n\n"
        f"{link}\n\n"
        f"O link vale por {minutes} minutos e só pode ser usado uma vez.\n\n"
        "Se você não pediu, ignore este e-mail.\n"
    )
    safe_link = escape(link)
    html = (
        "<p>Olá,</p>"
        "<p>Recebemos um pedido para redefinir a senha da sua conta no ÓleoApp. "
        "Para criar uma nova senha, use o link abaixo:</p>"
        f'<p><a href="{safe_link}">Redefinir minha senha</a></p>'
        f"<p>Se o link não abrir, copie e cole este endereço no navegador: {safe_link}</p>"
        f"<p>O link vale por {minutes} minutos e só pode ser usado uma vez.</p>"
        "<p>Se você não pediu, ignore este e-mail.</p>"
    )
    return text, html


async def forgot(db: AsyncDatabase, http: httpx.AsyncClient, email: str) -> None:
    """Gera e envia o link. Não revela se o e-mail existe e não propaga falha de envio."""
    user = await users.find_by_email(db, email)
    if user is None:
        return

    await password_resets.delete_unused_by_user(db, user["_id"])
    token = secrets.token_urlsafe(32)
    expires_at = _now() + timedelta(minutes=settings.PASSWORD_RESET_TTL_MIN)
    await password_resets.create(db, user["_id"], hash_token(token), expires_at)

    base_url = settings.FRONTEND_URL.rstrip("/")
    link = f"{base_url}/redefinir-senha?token={token}"
    text, html = _build_message(link, settings.PASSWORD_RESET_TTL_MIN)
    try:
        await send_email(http, user["email"], SUBJECT, text, html)
    except EmailSendError as exc:
        # Sem o token e sem o e-mail completo no log. A resposta da API continua sendo 204.
        logger.error(
            "Falha ao enviar e-mail de redefinição de senha para %s: %s",
            mask_email(user["email"]),
            exc,
        )


async def _forgot_safely(db: AsyncDatabase, http: httpx.AsyncClient, email: str) -> None:
    try:
        await forgot(db, http, email)
    except Exception as exc:
        # Só o tipo do erro e o e-mail mascarado: a mensagem poderia levar o token ou o e-mail.
        logger.error(
            "Falha ao processar o pedido de redefinição de senha para %s (%s)",
            mask_email(email),
            type(exc).__name__,
        )


def schedule_forgot(db: AsyncDatabase, http: httpx.AsyncClient, email: str) -> None:
    """Roda o forgot em segundo plano e volta na hora.

    Buscar o usuário, gerar o token e chamar o Brevo só acontece quando a conta existe;
    fora da requisição, o tempo de resposta não revela se o e-mail está cadastrado.
    """
    spawn(_forgot_safely(db, http, email))


async def reset(db: AsyncDatabase, token: str, new_password: str) -> None:
    record = await password_resets.find_valid(db, hash_token(token))
    if record is None:
        raise _invalid_token()

    # O Argon2 é CPU-bound: roda em thread para não travar o event loop.
    new_hash = await asyncio.to_thread(hash_password, new_password)

    # Uso único: só uma requisição consegue "gastar" o token, mesmo com pedidos simultâneos.
    if not await password_resets.mark_used(db, record["_id"]):
        raise _invalid_token()

    updated = await users.update_fields(
        db, record["user_id"], {"password_hash": new_hash, "updated_at": _now()}
    )
    if updated is None:
        raise _invalid_token()

    # Quem tinha a senha antiga (ou a conta invadida) perde o acesso: todas as sessões caem.
    await sessions.delete_by_user(db, record["user_id"])

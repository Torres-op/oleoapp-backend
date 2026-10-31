import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# O Render gratuito bloqueia SMTP (25, 465, 587): o envio é só pela API HTTP do Brevo.
BREVO_URL = "https://api.brevo.com/v3/smtp/email"


class EmailSendError(Exception):
    """Falha ao enviar e-mail. A mensagem nunca leva chave de API, token ou e-mail completo."""


def mask_email(email: str) -> str:
    """'ana@exemplo.com' -> 'a***@exemplo.com'. Use nos logs no lugar do e-mail completo."""
    local, separator, domain = email.rpartition("@")
    if not separator or not local:
        return "***"
    return f"{local[0]}***@{domain}"


async def send_email(http: httpx.AsyncClient, to: str, subject: str, text: str, html: str) -> None:
    if settings.EMAIL_PROVIDER == "console":
        if settings.ENV == "production":
            raise EmailSendError("console não permitido em produção")
        # Só fora de produção: serve para ver o link no desenvolvimento.
        # O destinatário vai mascarado, como em qualquer outro log.
        logger.info("E-mail (console) para %s: %s\n%s", mask_email(to), subject, text)
        return

    if not settings.BREVO_API_KEY or not settings.EMAIL_FROM:
        raise EmailSendError("Brevo sem chave de API ou sem remetente configurados")

    headers = {
        "api-key": settings.BREVO_API_KEY,
        "accept": "application/json",
        "content-type": "application/json",
    }
    payload = {
        "sender": {"name": settings.EMAIL_FROM_NAME, "email": settings.EMAIL_FROM},
        "to": [{"email": to}],
        "subject": subject,
        "textContent": text,
        "htmlContent": html,
    }
    try:
        response = await http.post(BREVO_URL, headers=headers, json=payload)
    except httpx.HTTPError as exc:
        raise EmailSendError(f"Falha de rede ao chamar o Brevo ({type(exc).__name__})") from exc
    if not response.is_success:
        raise EmailSendError(f"Brevo respondeu com status {response.status_code}")

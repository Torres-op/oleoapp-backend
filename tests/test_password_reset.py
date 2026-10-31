import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.core.background import drain
from app.core.config import settings
from app.core.security import hash_token
from app.db.collections import PASSWORD_RESETS, SESSIONS, USERS
from app.repositories import password_resets
from app.services import password_reset as password_reset_service
from app.services.email import BREVO_URL, EmailSendError, mask_email, send_email

EMAIL = "ana@exemplo.com"
PASSWORD = "Senha1234"
NEW_PASSWORD = "NovaSenha99"
FORGOT = "/api/auth/password/forgot"
RESET = "/api/auth/password/reset"
TOKEN_RE = re.compile(r"token=([A-Za-z0-9_-]+)")


class BrevoMock:
    """Faz o papel da API do Brevo: guarda as chamadas e responde como configurado."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.status = 201
        self.error: Exception | None = None
        self.client: httpx.AsyncClient

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, json={"messageId": "<teste@brevo>"})

    def payload(self, index: int = -1) -> dict:
        return json.loads(self.calls[index].content)

    def token(self, index: int = -1) -> str:
        match = TOKEN_RE.search(self.payload(index)["textContent"])
        assert match, "o e-mail não tem link com token"
        return match.group(1)


@pytest.fixture
def brevo(http_mock: Callable, monkeypatch: pytest.MonkeyPatch) -> BrevoMock:
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "brevo")
    monkeypatch.setattr(settings, "BREVO_API_KEY", "x")
    monkeypatch.setattr(settings, "EMAIL_FROM", "no-reply@exemplo.com")
    mock = BrevoMock()
    mock.client = http_mock(mock.handler)
    return mock


async def signup(client: httpx.AsyncClient, email: str = EMAIL) -> httpx.Response:
    response = await client.post(
        "/api/auth/signup", json={"name": "Ana Souza", "email": email, "password": PASSWORD}
    )
    assert response.status_code == 201
    return response


async def forgot(client: httpx.AsyncClient, email: str = EMAIL) -> httpx.Response:
    """Pede o link e espera o processamento em segundo plano (busca do usuário e envio)."""
    response = await client.post(FORGOT, json={"email": email})
    await drain(timeout=5)
    return response


async def reset(
    client: httpx.AsyncClient, token: str, new_password: str = NEW_PASSWORD
) -> httpx.Response:
    return await client.post(RESET, json={"token": token, "new_password": new_password})


async def login(client: httpx.AsyncClient, password: str) -> httpx.Response:
    return await client.post("/api/auth/login", json={"email": EMAIL, "password": password})


def error_code(response: httpx.Response) -> str:
    return response.json()["error"]["code"]


# --- mask_email e send_email ------------------------------------------------


def test_mask_email_esconde_o_meio_do_endereco():
    assert mask_email("ana@exemplo.com") == "a***@exemplo.com"
    assert mask_email("sem-arroba") == "***"
    assert mask_email("@exemplo.com") == "***"


@pytest.mark.parametrize("setting", ["BREVO_API_KEY", "EMAIL_FROM"])
async def test_send_email_sem_chave_ou_remetente_levanta_erro_e_nao_chama_o_brevo(
    brevo: BrevoMock, monkeypatch: pytest.MonkeyPatch, setting: str
):
    monkeypatch.setattr(settings, setting, "")

    with pytest.raises(EmailSendError):
        await send_email(brevo.client, EMAIL, "Assunto", "texto", "<p>html</p>")

    assert brevo.calls == []


async def test_send_email_manda_o_pedido_no_formato_da_api_do_brevo(brevo: BrevoMock):
    await send_email(brevo.client, EMAIL, "Assunto", "texto", "<p>html</p>")

    assert len(brevo.calls) == 1
    request = brevo.calls[0]
    assert request.method == "POST"
    assert str(request.url) == BREVO_URL == "https://api.brevo.com/v3/smtp/email"
    assert request.headers["api-key"] == "x"
    assert request.headers["accept"] == "application/json"
    assert request.headers["content-type"] == "application/json"
    assert brevo.payload() == {
        "sender": {"name": settings.EMAIL_FROM_NAME, "email": "no-reply@exemplo.com"},
        "to": [{"email": EMAIL}],
        "subject": "Assunto",
        "textContent": "texto",
        "htmlContent": "<p>html</p>",
    }


@pytest.mark.parametrize("status", [400, 401, 429, 500])
async def test_send_email_com_status_fora_de_2xx_levanta_erro(brevo: BrevoMock, status: int):
    brevo.status = status
    with pytest.raises(EmailSendError):
        await send_email(brevo.client, EMAIL, "Assunto", "texto", "<p>html</p>")


async def test_send_email_com_erro_de_rede_levanta_erro(brevo: BrevoMock):
    brevo.error = httpx.ConnectError("sem rede")
    with pytest.raises(EmailSendError):
        await send_email(brevo.client, EMAIL, "Assunto", "texto", "<p>html</p>")


# --- pedido do link (forgot) ------------------------------------------------


async def test_forgot_com_email_inexistente_responde_204_sem_chamar_o_brevo(
    client: httpx.AsyncClient, app: FastAPI, brevo: BrevoMock
):
    response = await forgot(client, "ninguem@exemplo.com")

    assert response.status_code == 204
    assert response.content == b""
    assert brevo.calls == []
    assert await app.state.db[PASSWORD_RESETS].count_documents({}) == 0


async def test_forgot_com_email_existente_responde_204_e_envia_um_email_com_o_link(
    client: httpx.AsyncClient, app: FastAPI, brevo: BrevoMock
):
    await signup(client)

    response = await forgot(client)

    assert response.status_code == 204
    assert response.content == b""
    assert len(brevo.calls) == 1
    assert brevo.calls[0].headers["api-key"] == "x"
    payload = brevo.payload()
    assert payload["to"] == [{"email": EMAIL}]
    assert payload["subject"] == "Redefinição de senha — ÓleoApp"
    token = brevo.token()
    link = f"{settings.FRONTEND_URL}/redefinir-senha?token={token}"
    assert link in payload["textContent"]
    assert link in payload["htmlContent"]
    assert f"{settings.PASSWORD_RESET_TTL_MIN} minutos" in payload["textContent"]
    assert "Se você não pediu, ignore este e-mail." in payload["textContent"]
    assert "<img" not in payload["htmlContent"]

    # O banco guarda só o hash do token, com a validade configurada.
    records = [doc async for doc in app.state.db[PASSWORD_RESETS].find()]
    assert len(records) == 1
    record = records[0]
    assert record["token_hash"] == hash_token(token)
    assert token not in str(record)
    assert record["used_at"] is None
    ttl = record["expires_at"] - record["created_at"]
    assert abs(ttl - timedelta(minutes=settings.PASSWORD_RESET_TTL_MIN)) < timedelta(seconds=5)


async def test_forgot_normaliza_o_email_informado(client: httpx.AsyncClient, brevo: BrevoMock):
    await signup(client)

    response = await forgot(client, "  ANA@Exemplo.COM ")

    assert response.status_code == 204
    assert len(brevo.calls) == 1
    assert brevo.payload()["to"] == [{"email": EMAIL}]


async def test_novo_pedido_invalida_o_link_anterior(
    client: httpx.AsyncClient, app: FastAPI, brevo: BrevoMock
):
    await signup(client)
    await forgot(client)
    first_token = brevo.token()
    await forgot(client)
    second_token = brevo.token()

    assert first_token != second_token
    assert await app.state.db[PASSWORD_RESETS].count_documents({}) == 1
    assert error_code(await reset(client, first_token)) == "invalid_token"
    assert (await reset(client, second_token)).status_code == 204


async def test_quarto_pedido_para_o_mesmo_email_na_hora_responde_429(
    client: httpx.AsyncClient, brevo: BrevoMock
):
    await signup(client)

    for _ in range(3):
        assert (await forgot(client)).status_code == 204
    blocked = await forgot(client)

    assert blocked.status_code == 429
    assert error_code(blocked) == "rate_limited"
    assert int(blocked.headers["Retry-After"]) >= 1
    assert len(brevo.calls) == 3


async def test_limite_por_email_vale_tambem_para_email_inexistente(
    client: httpx.AsyncClient, brevo: BrevoMock
):
    # Mesma resposta para quem tem e para quem não tem conta: nada revela se o e-mail existe.
    for _ in range(3):
        assert (await forgot(client, "ninguem@exemplo.com")).status_code == 204
    blocked = await forgot(client, "ninguem@exemplo.com")

    assert blocked.status_code == 429
    assert error_code(blocked) == "rate_limited"
    assert brevo.calls == []


async def test_forgot_tem_rate_limit_por_ip(
    client: httpx.AsyncClient, brevo: BrevoMock, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "RATE_LIMIT_PASSWORD_FORGOT_PER_HOUR", 2)

    assert (await forgot(client, "um@exemplo.com")).status_code == 204
    assert (await forgot(client, "dois@exemplo.com")).status_code == 204
    blocked = await forgot(client, "tres@exemplo.com")

    assert blocked.status_code == 429
    assert error_code(blocked) == "rate_limited"
    assert "Retry-After" in blocked.headers


@pytest.mark.parametrize(
    "body",
    [
        {"email": "isto-nao-e-email"},
        {"email": ""},
        {},
        {"email": EMAIL, "extra": 1},
    ],
)
async def test_forgot_com_dados_invalidos_responde_422(
    client: httpx.AsyncClient, brevo: BrevoMock, body: dict
):
    response = await client.post(FORGOT, json=body)

    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert brevo.calls == []


async def test_forgot_com_brevo_respondendo_500_continua_204_e_loga_sem_segredos(
    client: httpx.AsyncClient, brevo: BrevoMock, caplog: pytest.LogCaptureFixture
):
    await signup(client)
    brevo.status = 500
    caplog.set_level(logging.INFO)

    response = await forgot(client)

    assert response.status_code == 204
    assert len(brevo.calls) == 1
    token = brevo.token()
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    logged = caplog.text
    assert "a***@exemplo.com" in logged
    assert EMAIL not in logged
    assert token not in logged
    assert "api-key" not in logged


async def test_forgot_com_erro_de_rede_no_brevo_continua_204(
    client: httpx.AsyncClient, brevo: BrevoMock, caplog: pytest.LogCaptureFixture
):
    await signup(client)
    brevo.error = httpx.ConnectError("sem rede")

    response = await forgot(client)

    assert response.status_code == 204
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert EMAIL not in caplog.text


async def test_provider_console_em_producao_nao_envia_e_loga_erro(
    client: httpx.AsyncClient,
    brevo: BrevoMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    await signup(client)
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "console")
    monkeypatch.setattr(settings, "ENV", "production")
    caplog.set_level(logging.INFO)

    response = await forgot(client)

    assert response.status_code == 204
    assert brevo.calls == []
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "console não permitido em produção" in errors[0].getMessage()
    assert EMAIL not in caplog.text
    assert "E-mail (console)" not in caplog.text


async def test_provider_console_fora_de_producao_loga_o_link_e_nao_chama_o_brevo(
    client: httpx.AsyncClient,
    brevo: BrevoMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    await signup(client)
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "console")
    caplog.set_level(logging.INFO)

    response = await forgot(client)

    assert response.status_code == 204
    assert brevo.calls == []
    assert "E-mail (console) para" in caplog.text
    assert f"{settings.FRONTEND_URL}/redefinir-senha?token=" in caplog.text


async def test_provider_console_loga_o_email_mascarado_e_o_assunto(
    client: httpx.AsyncClient,
    brevo: BrevoMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    await signup(client)
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "console")
    caplog.set_level(logging.INFO)

    await forgot(client)

    assert "E-mail (console) para a***@exemplo.com: Redefinição de senha — ÓleoApp" in caplog.text
    assert EMAIL not in caplog.text


# --- envio em segundo plano -------------------------------------------------


async def test_forgot_responde_antes_de_enviar_o_email(
    client: httpx.AsyncClient, brevo: BrevoMock, http_mock: Callable
):
    await signup(client)
    release = asyncio.Event()
    sent: list[httpx.Request] = []

    async def slow_brevo(request: httpx.Request) -> httpx.Response:
        await release.wait()
        sent.append(request)
        return httpx.Response(201, json={"messageId": "<teste@brevo>"})

    http_mock(slow_brevo)

    # O Brevo está "travado": se a rota esperasse o envio, o post não voltaria.
    response = await asyncio.wait_for(client.post(FORGOT, json={"email": EMAIL}), timeout=5)

    assert response.status_code == 204
    assert sent == []
    release.set()
    await drain(timeout=5)
    assert len(sent) == 1


async def test_forgot_com_e_sem_conta_responde_igual_e_sem_esperar(
    client: httpx.AsyncClient, brevo: BrevoMock, monkeypatch: pytest.MonkeyPatch
):
    await signup(client)
    scheduled: list[str] = []
    monkeypatch.setattr(
        password_reset_service, "schedule_forgot", lambda db, http, email: scheduled.append(email)
    )

    existing = await client.post(FORGOT, json={"email": EMAIL})
    missing = await client.post(FORGOT, json={"email": "ninguem@exemplo.com"})

    # A rota não consulta o usuário: só agenda o trabalho e devolve a mesma resposta.
    assert existing.status_code == missing.status_code == 204
    assert existing.content == missing.content == b""
    assert scheduled == [EMAIL, "ninguem@exemplo.com"]


async def test_erro_inesperado_em_segundo_plano_e_logado_sem_segredos(
    client: httpx.AsyncClient,
    brevo: BrevoMock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    await signup(client)

    async def broken_find_by_email(db, email):
        raise RuntimeError(f"falha ao buscar {email}")

    monkeypatch.setattr(password_reset_service.users, "find_by_email", broken_find_by_email)

    response = await forgot(client)

    assert response.status_code == 204
    assert brevo.calls == []
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "a***@exemplo.com" in errors[0].getMessage()
    assert "RuntimeError" in errors[0].getMessage()
    assert EMAIL not in caplog.text


async def test_limite_por_email_vem_da_configuracao(
    client: httpx.AsyncClient, brevo: BrevoMock, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "RATE_LIMIT_PASSWORD_FORGOT_PER_EMAIL_PER_HOUR", 1)
    await signup(client)

    assert (await forgot(client)).status_code == 204
    blocked = await forgot(client)

    assert blocked.status_code == 429
    assert error_code(blocked) == "rate_limited"
    assert len(brevo.calls) == 1


# --- troca da senha (reset) -------------------------------------------------


async def test_reset_com_token_valido_troca_a_senha_e_derruba_as_sessoes(
    client: httpx.AsyncClient, app: FastAPI, brevo: BrevoMock
):
    user_id = ObjectId((await signup(client)).json()["id"])
    old_hash = (await app.state.db[USERS].find_one({"_id": user_id}))["password_hash"]
    assert (await client.get("/api/auth/me")).status_code == 200
    await forgot(client)

    response = await reset(client, brevo.token())

    assert response.status_code == 204
    assert response.content == b""
    user = await app.state.db[USERS].find_one({"_id": user_id})
    assert user["password_hash"] != old_hash
    assert user["updated_at"] >= user["created_at"]
    # A sessão aberta no cadastro caiu, no banco e para o cliente.
    assert await app.state.db[SESSIONS].count_documents({"user_id": user_id}) == 0
    assert (await client.get("/api/auth/me")).status_code == 401
    # A senha nova funciona e a antiga não.
    assert (await login(client, NEW_PASSWORD)).status_code == 200
    assert error_code(await login(client, PASSWORD)) == "invalid_credentials"
    record = await app.state.db[PASSWORD_RESETS].find_one({"user_id": user_id})
    assert record["used_at"] is not None


async def test_reset_derruba_as_sessoes_de_todos_os_dispositivos(
    client: httpx.AsyncClient, app: FastAPI, brevo: BrevoMock
):
    user_id = ObjectId((await signup(client)).json()["id"])
    await login(client, PASSWORD)  # segunda sessão
    assert await app.state.db[SESSIONS].count_documents({"user_id": user_id}) == 2
    await forgot(client)

    await reset(client, brevo.token())

    assert await app.state.db[SESSIONS].count_documents({"user_id": user_id}) == 0


async def test_reset_com_token_reutilizado_responde_400_invalid_token(
    client: httpx.AsyncClient, brevo: BrevoMock
):
    await signup(client)
    await forgot(client)
    token = brevo.token()
    assert (await reset(client, token)).status_code == 204

    response = await reset(client, token, "OutraSenha77")

    assert response.status_code == 400
    assert error_code(response) == "invalid_token"
    assert response.json()["error"]["message"] == "Link inválido ou expirado. Peça um novo."
    # A segunda tentativa não trocou a senha.
    assert (await login(client, NEW_PASSWORD)).status_code == 200
    assert (await login(client, "OutraSenha77")).status_code == 401


async def test_reset_com_token_expirado_responde_400_invalid_token(
    client: httpx.AsyncClient, app: FastAPI
):
    user_id = ObjectId((await signup(client)).json()["id"])
    token = "expirado" * 6
    await password_resets.create(
        app.state.db, user_id, hash_token(token), datetime.now(UTC) - timedelta(minutes=1)
    )

    response = await reset(client, token)

    assert response.status_code == 400
    assert error_code(response) == "invalid_token"
    assert (await login(client, PASSWORD)).status_code == 200


async def test_reset_com_token_inventado_responde_400_invalid_token(client: httpx.AsyncClient):
    response = await reset(client, "inventado" * 5)

    assert response.status_code == 400
    assert error_code(response) == "invalid_token"


@pytest.mark.parametrize("token", ["curto", "a" * 201])
async def test_reset_com_token_fora_do_tamanho_responde_422(client: httpx.AsyncClient, token: str):
    response = await reset(client, token)

    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert response.json()["error"]["fields"][0]["field"] == "token"


@pytest.mark.parametrize("weak", ["curta1A", "semnumeroAAAA", "semmaiuscula1", "a" * 129 + "A1"])
async def test_reset_com_senha_fraca_responde_422_e_nao_gasta_o_token(
    client: httpx.AsyncClient, brevo: BrevoMock, weak: str
):
    await signup(client)
    await forgot(client)
    token = brevo.token()

    response = await reset(client, token, weak)

    assert response.status_code == 422
    assert error_code(response) == "validation_error"
    assert response.json()["error"]["fields"][0]["field"] == "new_password"
    # O link continua valendo para uma senha boa.
    assert (await reset(client, token)).status_code == 204
    assert (await login(client, NEW_PASSWORD)).status_code == 200


async def test_reset_com_campo_extra_responde_422(client: httpx.AsyncClient):
    response = await client.post(
        RESET, json={"token": "t" * 30, "new_password": NEW_PASSWORD, "extra": 1}
    )

    assert response.status_code == 422
    assert error_code(response) == "validation_error"


async def test_excluir_a_conta_apaga_os_links_pendentes(
    client: httpx.AsyncClient, app: FastAPI, brevo: BrevoMock
):
    await signup(client)
    await forgot(client)
    assert await app.state.db[PASSWORD_RESETS].count_documents({}) == 1

    response = await client.post("/api/auth/delete-account", json={"current_password": PASSWORD})

    assert response.status_code == 204
    assert await app.state.db[PASSWORD_RESETS].count_documents({}) == 0

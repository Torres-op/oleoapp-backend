from collections.abc import Callable

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.core.config import settings
from app.core.security import verify_password
from app.db.collections import AUDIT_LOGS, USERS
from app.repositories import audit as audit_repo
from scripts import create_admin
from tests.test_auth import SIGNUP, extra_client, login, signup
from tests.test_oauth import ProvidersMock, flow

UA = "Mozilla/5.0 (Teste)"


async def events(app: FastAPI, **query) -> list[dict]:
    return await app.state.db[AUDIT_LOGS].find(query).sort("created_at", 1).to_list(length=None)


def assert_no_secrets(document: dict, *secrets: str) -> None:
    text = repr(document)
    for secret in secrets:
        assert secret not in text
    assert not any("password" in key or "token" in key for key in document)


# --- fluxos de conta ------------------------------------------------------------------


async def test_cadastro_grava_evento_com_ip_e_navegador(app: FastAPI, client: httpx.AsyncClient):
    response = await client.post("/api/auth/signup", json=SIGNUP, headers={"User-Agent": UA})
    user_id = response.json()["id"]

    (event,) = await events(app, type="signup")
    assert event["user_id"] == ObjectId(user_id)
    assert event["email"] == "ana@exemplo.com"
    assert event["method"] == "password"
    assert event["success"] is True
    assert event["user_agent"] == UA
    assert event["ip"]
    assert_no_secrets(event, SIGNUP["password"])


async def test_login_ok_grava_evento_e_ultimo_login(app: FastAPI, client: httpx.AsyncClient):
    await signup(client)
    async with extra_client(app) as other:
        assert (await login(other, "ana@exemplo.com", SIGNUP["password"])).status_code == 200

    (event,) = await events(app, type="login_success")
    assert event["method"] == "password"
    assert event["email"] == "ana@exemplo.com"
    user = await app.state.db[USERS].find_one({"email": "ana@exemplo.com"})
    assert user["last_login_at"] is not None
    assert_no_secrets(event, SIGNUP["password"])


async def test_login_com_falha_mascara_o_email(app: FastAPI, client: httpx.AsyncClient):
    await signup(client)
    async with extra_client(app) as other:
        assert (await login(other, "ana@exemplo.com", "SenhaErrada1")).status_code == 401

    (event,) = await events(app, type="login_failure")
    assert event["email"] == "a***@exemplo.com"
    assert event["success"] is False
    assert event["reason"] == "invalid_credentials"
    assert event["user_id"] is None
    assert_no_secrets(event, "SenhaErrada1", "ana@exemplo.com")


async def test_login_bloqueado_por_rate_limit_fica_registrado(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    if app.state.redis is None:
        pytest.skip("Sem Redis não há rate limit.")
    monkeypatch.setattr(settings, "RATE_LIMIT_LOGIN_PER_MIN", 1)
    await login(client, "ana@exemplo.com", "Qualquer1")
    assert (await login(client, "ana@exemplo.com", "Qualquer1")).status_code == 429

    reasons = [event["reason"] for event in await events(app, type="login_failure")]
    assert reasons == ["invalid_credentials", "rate_limited"]


async def test_logout_grava_evento(app: FastAPI, client: httpx.AsyncClient):
    user_id = (await signup(client)).json()["id"]
    assert (await client.post("/api/auth/logout")).status_code == 204
    (event,) = await events(app, type="logout")
    assert event["user_id"] == ObjectId(user_id)


async def test_logout_sem_sessao_nao_grava_evento(app: FastAPI, client: httpx.AsyncClient):
    assert (await client.post("/api/auth/logout")).status_code == 204
    assert await events(app, type="logout") == []


async def test_exclusao_anonimiza_os_eventos_do_usuario(app: FastAPI, client: httpx.AsyncClient):
    user_id = ObjectId((await signup(client)).json()["id"])
    await client.post("/api/disposals", json={"volume_ml": 1000})
    async with extra_client(app) as bruno:
        bruno_id = ObjectId(
            (await signup(bruno, name="Bruno Lima", email="bruno@exemplo.com")).json()["id"]
        )

    response = await client.post(
        "/api/auth/delete-account", json={"current_password": SIGNUP["password"]}
    )
    assert response.status_code == 204

    assert await events(app, user_id=user_id) == []
    anonymized = await events(app, anonymized=True)
    assert {event["type"] for event in anonymized} == {
        "signup",
        "disposal_created",
        "account_deleted",
    }
    assert all(event["user_id"] is None and event["email"] is None for event in anonymized)
    # Os eventos de outras pessoas continuam intactos.
    (bruno_signup,) = await events(app, user_id=bruno_id)
    assert bruno_signup["email"] == "bruno@exemplo.com"


async def test_descarte_grava_evento_e_devolve_impacto(app: FastAPI, client: httpx.AsyncClient):
    user_id = (await signup(client)).json()["id"]
    response = await client.post("/api/disposals", json={"volume_ml": 2500})

    assert response.status_code == 201
    assert response.json()["impact"] == {"water_l": 2_500_000.0, "co2e_kg": 2.5}
    (event,) = await events(app, type="disposal_created")
    assert event["user_id"] == ObjectId(user_id)
    assert event["reason"] is None

    listed = (await client.get("/api/disposals")).json()["items"][0]
    assert listed["impact"] == {"water_l": 2_500_000.0, "co2e_kg": 2.5}


async def test_falha_ao_gravar_auditoria_nao_quebra_o_login(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    async def broken_insert(db, document):
        raise RuntimeError("banco de auditoria fora do ar")

    monkeypatch.setattr(audit_repo, "insert", broken_insert)
    assert (await signup(client)).status_code == 201
    async with extra_client(app) as other:
        assert (await login(other, "ana@exemplo.com", SIGNUP["password"])).status_code == 200


async def test_login_google_grava_cadastro_e_login(
    app: FastAPI,
    client: httpx.AsyncClient,
    http_mock: Callable,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "FRONTEND_URL", "http://localhost:3000")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "id-google")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "segredo-google")
    http_mock(ProvidersMock().handler)

    await flow(client, "google")

    methods = {(event["type"], event["method"]) for event in await events(app)}
    assert ("signup", "google") in methods
    assert ("login_success", "google") in methods
    user = await app.state.db[USERS].find_one({"email": "ana@exemplo.com"})
    assert user["last_login_at"] is not None


# --- script do primeiro admin -------------------------------------------------------------


@pytest.fixture
def no_admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)


def fake_getpass(monkeypatch: pytest.MonkeyPatch, *answers: str) -> None:
    replies = iter(answers)
    monkeypatch.setattr(create_admin.getpass, "getpass", lambda prompt="": next(replies))


async def test_script_cria_admin_novo(app: FastAPI, no_admin_env, monkeypatch):
    fake_getpass(monkeypatch, "Chefe12345", "Chefe12345")

    assert await create_admin.run("chefe@exemplo.com", name="Chefe") == 0

    user = await app.state.db[USERS].find_one({"email": "chefe@exemplo.com"})
    assert user["role"] == "admin"
    assert user["name"] == "Chefe"
    assert verify_password("Chefe12345", user["password_hash"])
    (event,) = await events(app, type="admin_promoted")
    assert event["user_id"] == user["_id"]
    assert event["reason"] == "script"
    assert_no_secrets(event, "Chefe12345")


async def test_script_promove_conta_existente_sem_pedir_senha(
    app: FastAPI, client: httpx.AsyncClient, no_admin_env, monkeypatch
):
    await signup(client)

    def must_not_ask(prompt: str = "") -> str:
        raise AssertionError("não deveria pedir senha")

    monkeypatch.setattr(create_admin.getpass, "getpass", must_not_ask)
    assert await create_admin.run("ana@exemplo.com") == 0
    assert (await client.get("/api/auth/me")).json()["role"] == "admin"

    assert await create_admin.run("ana@exemplo.com", revoke=True) == 0
    assert (await client.get("/api/auth/me")).json()["role"] == "user"
    assert [event["type"] for event in await events(app, reason="script")] == [
        "admin_promoted",
        "admin_revoked",
    ]


async def test_script_usa_a_senha_do_ambiente(app: FastAPI, no_admin_env, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "Ambiente123")
    assert await create_admin.run("env@exemplo.com") == 0
    user = await app.state.db[USERS].find_one({"email": "env@exemplo.com"})
    assert verify_password("Ambiente123", user["password_hash"])


@pytest.mark.parametrize("answers", [("fraca", "fraca"), ("Chefe12345", "Outra12345")])
async def test_script_rejeita_senha_fraca_ou_diferente(
    app: FastAPI, no_admin_env, monkeypatch, answers: tuple[str, str]
):
    fake_getpass(monkeypatch, *answers)
    assert await create_admin.run("chefe@exemplo.com") == 1
    assert await app.state.db[USERS].count_documents({}) == 0


async def test_script_revoke_de_conta_inexistente(app: FastAPI, no_admin_env):
    assert await create_admin.run("ninguem@exemplo.com", revoke=True) == 1


def test_script_recusa_email_invalido(no_admin_env, capsys: pytest.CaptureFixture[str]):
    assert create_admin.main(["--email", "nao-e-email"]) == 1
    assert "E-mail inválido" in capsys.readouterr().out

import base64
import hashlib
from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import settings
from app.core.security import hash_token
from app.db.collections import OAUTH_PENDING_LINKS, OAUTH_STATES, SESSIONS, USERS
from tests.test_auth import extra_client, login, signup

FRONT = "http://localhost:3000"
GOOGLE_SECRET = "segredo-google"


class ProvidersMock:
    """Simula os endpoints de token e de perfil do Google."""

    def __init__(self) -> None:
        self.google_profile: dict = {
            "sub": "g-123",
            "email": "Ana@Exemplo.com",
            "email_verified": True,
            "name": "Ana Souza",
        }
        self.token_status = 200
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "oauth2.googleapis.com":
            return httpx.Response(self.token_status, json={"access_token": "tok-google"})
        if url.host == "openidconnect.googleapis.com":
            return httpx.Response(200, json=self.google_profile)
        return httpx.Response(404)

    def find(self, host: str, path_end: str = "") -> httpx.Request:
        return next(
            r for r in self.requests if r.url.host == host and r.url.path.endswith(path_end)
        )


@pytest.fixture(autouse=True)
def _credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "FRONTEND_URL", FRONT)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "id-google")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", GOOGLE_SECRET)


@pytest.fixture
def providers(http_mock: Callable) -> ProvidersMock:
    mock = ProvidersMock()
    http_mock(mock.handler)
    return mock


async def start(client: httpx.AsyncClient, provider: str, next_path: str = "/favoritos"):
    response = await client.get(f"/api/auth/oauth/{provider}/start", params={"next": next_path})
    assert response.status_code == 302
    url = httpx.URL(response.headers["location"])
    return response, url


async def flow(client: httpx.AsyncClient, provider: str, next_path: str = "/favoritos"):
    """start + callback com o state certo. Devolve a resposta do callback."""
    _, url = await start(client, provider, next_path)
    return await client.get(
        f"/api/auth/oauth/{provider}/callback",
        params={"code": "codigo-123", "state": url.params["state"]},
    )


def location(response: httpx.Response) -> str:
    assert response.status_code == 302
    return response.headers["location"]


# --- provedores e início do fluxo ---------------------------------------------


async def test_providers_mostra_so_os_configurados(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    assert (await client.get("/api/auth/providers")).json() == {"google": True}

    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "")
    assert (await client.get("/api/auth/providers")).json() == {"google": False}


async def test_inicio_google_usa_pkce_e_grava_o_state(app: FastAPI, client: httpx.AsyncClient):
    response, url = await start(client, "google")

    assert url.host == "accounts.google.com"
    params = url.params
    assert params["client_id"] == "id-google"
    assert params["redirect_uri"] == f"{FRONT}/api/auth/oauth/google/callback"
    assert params["response_type"] == "code"
    assert params["scope"] == "openid email profile"
    assert params["prompt"] == "select_account"
    assert params["code_challenge_method"] == "S256"

    record = await app.state.db[OAUTH_STATES].find_one({"state_hash": hash_token(params["state"])})
    assert record["provider"] == "google"
    assert record["next"] == "/favoritos"
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(record["code_verifier"].encode()).digest())
        .rstrip(b"=")
        .decode()
    )
    assert params["code_challenge"] == challenge

    cookie = response.headers["set-cookie"].lower()
    assert cookie.startswith("oleo_oauth_state=")
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert "path=/api/auth/oauth" in cookie
    assert "max-age=600" in cookie


@pytest.mark.parametrize("bad_next", ["//evil.com", "https://evil.com", "/\\evil.com", "mapa"])
async def test_next_invalido_vira_o_mapa(
    client: httpx.AsyncClient, providers: ProvidersMock, bad_next: str
):
    response = await flow(client, "google", bad_next)
    assert location(response) == f"{FRONT}/map"


# O Facebook foi retirado: hoje ele cai no mesmo caso de um provedor desconhecido.
@pytest.mark.parametrize("provider", ["google", "facebook", "twitter"])
async def test_provedor_sem_credenciais_volta_ao_login(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, provider: str
):
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "")
    response = await client.get(f"/api/auth/oauth/{provider}/start")
    assert location(response) == f"{FRONT}/login?erro=oauth_indisponivel"


# --- callback ----------------------------------------------------------------------


async def test_google_novo_usuario_entra_e_vai_para_o_next(
    app: FastAPI, client: httpx.AsyncClient, providers: ProvidersMock
):
    response = await flow(client, "google")

    assert location(response) == f"{FRONT}/favoritos"
    assert client.cookies.get(settings.SESSION_COOKIE_NAME)
    assert "oleo_oauth_state=" in response.headers.get_list("set-cookie")[0]

    user = await app.state.db[USERS].find_one({"email": "ana@exemplo.com"})
    assert user["password_hash"] is None
    assert user["oauth"] == {"google": "g-123"}
    assert user["name"] == "Ana Souza"

    token_request = providers.find("oauth2.googleapis.com")
    assert b"code_verifier=" in token_request.content
    assert b"code=codigo-123" in token_request.content

    me = (await client.get("/api/auth/me")).json()
    assert me["has_password"] is False
    assert me["providers"] == ["google"]
    # O state é de uso único.
    assert await app.state.db[OAUTH_STATES].count_documents({}) == 0


async def test_usuario_ja_vinculado_entra_sem_duplicar(
    app: FastAPI, client: httpx.AsyncClient, providers: ProvidersMock
):
    await flow(client, "google")
    async with extra_client(app) as other:
        response = await flow(other, "google", "/dashboard")
        assert location(response) == f"{FRONT}/dashboard"
        assert other.cookies.get(settings.SESSION_COOKIE_NAME)
    assert await app.state.db[USERS].count_documents({}) == 1
    assert await app.state.db[SESSIONS].count_documents({}) == 2


async def test_state_diferente_ou_sem_cookie_falha(
    app: FastAPI, client: httpx.AsyncClient, providers: ProvidersMock
):
    _, url = await start(client, "google")
    tampered = await client.get(
        "/api/auth/oauth/google/callback", params={"code": "c", "state": "outro"}
    )
    assert location(tampered) == f"{FRONT}/login?erro=oauth_falhou"

    async with extra_client(app) as no_cookie:
        response = await no_cookie.get(
            "/api/auth/oauth/google/callback",
            params={"code": "c", "state": url.params["state"]},
        )
    assert location(response) == f"{FRONT}/login?erro=oauth_falhou"
    assert providers.requests == []
    assert await app.state.db[USERS].count_documents({}) == 0


async def test_state_nao_pode_ser_reusado(client: httpx.AsyncClient, providers: ProvidersMock):
    _, url = await start(client, "google")
    params = {"code": "c", "state": url.params["state"]}
    first = await client.get("/api/auth/oauth/google/callback", params=params)
    assert location(first) == f"{FRONT}/favoritos"
    second = await client.get("/api/auth/oauth/google/callback", params=params)
    assert location(second) == f"{FRONT}/login?erro=oauth_falhou"


async def test_usuario_cancelou_no_provedor(client: httpx.AsyncClient, providers: ProvidersMock):
    _, url = await start(client, "google")
    response = await client.get(
        "/api/auth/oauth/google/callback",
        params={"error": "access_denied", "state": url.params["state"]},
    )
    assert location(response) == f"{FRONT}/login?erro=oauth_cancelado"
    assert providers.requests == []


async def test_troca_do_code_falhando(
    app: FastAPI, client: httpx.AsyncClient, providers: ProvidersMock
):
    providers.token_status = 400
    response = await flow(client, "google")
    assert location(response) == f"{FRONT}/login?erro=oauth_falhou"
    assert not client.cookies.get(settings.SESSION_COOKIE_NAME)
    assert await app.state.db[USERS].count_documents({}) == 0


async def test_provedor_sem_email(client: httpx.AsyncClient, providers: ProvidersMock):
    providers.google_profile = {"sub": "g-123", "name": "Ana"}
    response = await flow(client, "google")
    assert location(response) == f"{FRONT}/login?erro=oauth_sem_email"


async def test_google_com_email_verificado_vincula_conta_existente(
    app: FastAPI, client: httpx.AsyncClient, providers: ProvidersMock
):
    await signup(client)
    existing = await app.state.db[USERS].find_one({"email": "ana@exemplo.com"})

    async with extra_client(app) as other:
        response = await flow(other, "google")
        assert location(response) == f"{FRONT}/favoritos"
        me = (await other.get("/api/auth/me")).json()

    assert me["id"] == str(existing["_id"])
    assert me["has_password"] is True
    assert me["providers"] == ["google"]
    assert await app.state.db[USERS].count_documents({}) == 1


async def test_google_sem_email_verificado_nao_vincula(
    app: FastAPI, client: httpx.AsyncClient, providers: ProvidersMock
):
    await signup(client)
    providers.google_profile["email_verified"] = False

    async with extra_client(app) as other:
        response = await flow(other, "google")
        assert location(response) == f"{FRONT}/login?erro=oauth_vincular&provedor=google"
        assert not other.cookies.get(settings.SESSION_COOKIE_NAME)

    user = await app.state.db[USERS].find_one({"email": "ana@exemplo.com"})
    assert "oauth" not in user
    assert await app.state.db[OAUTH_PENDING_LINKS].count_documents({}) == 1


async def test_email_nao_verificado_vincula_no_proximo_login_com_senha(
    app: FastAPI, client: httpx.AsyncClient, providers: ProvidersMock
):
    await signup(client)
    providers.google_profile["email_verified"] = False

    async with extra_client(app) as other:
        response = await flow(other, "google")
        assert location(response) == f"{FRONT}/login?erro=oauth_vincular&provedor=google"
        link_cookie = next(
            c for c in response.headers.get_list("set-cookie") if c.startswith("oleo_oauth_link=")
        ).lower()
        assert "httponly" in link_cookie and "path=/api/auth" in link_cookie

        logged = await login(other, "ana@exemplo.com", "Senha1234")
        assert logged.status_code == 200
        assert logged.json()["providers"] == ["google"]
        assert any(
            c.startswith("oleo_oauth_link=") and "max-age=0" in c.lower()
            for c in logged.headers.get_list("set-cookie")
        )

    user = await app.state.db[USERS].find_one({"email": "ana@exemplo.com"})
    assert user["oauth"] == {"google": "g-123"}
    assert await app.state.db[OAUTH_PENDING_LINKS].count_documents({}) == 0

    # Daqui em diante o Google entra direto.
    async with extra_client(app) as third:
        assert location(await flow(third, "google")) == f"{FRONT}/favoritos"


async def test_vinculo_pendente_nao_vale_para_outra_conta(
    app: FastAPI, client: httpx.AsyncClient, providers: ProvidersMock
):
    await signup(client)
    async with extra_client(app) as bruno:
        await signup(bruno, name="Bruno Lima", email="bruno@exemplo.com")

    providers.google_profile["email_verified"] = False
    async with extra_client(app) as other:
        await flow(other, "google")  # pendente para a conta da Ana
        logged = await login(other, "bruno@exemplo.com", "Senha1234")
        assert logged.json()["providers"] == []

    bruno_doc = await app.state.db[USERS].find_one({"email": "bruno@exemplo.com"})
    assert "oauth" not in bruno_doc


async def test_nome_vazio_usa_o_inicio_do_email(
    app: FastAPI, client: httpx.AsyncClient, providers: ProvidersMock
):
    providers.google_profile = {
        "sub": "g-9",
        "email": "joao.silva@exemplo.com",
        "email_verified": True,
        "name": " ",
    }
    await flow(client, "google")
    user = await app.state.db[USERS].find_one({"email": "joao.silva@exemplo.com"})
    assert user["name"] == "joao.silva"


# --- contas sem senha --------------------------------------------------------------


async def test_conta_sem_senha_nao_entra_com_senha(
    client: httpx.AsyncClient, providers: ProvidersMock, app: FastAPI
):
    await flow(client, "google")
    async with extra_client(app) as other:
        response = await login(other, "ana@exemplo.com", "Senha1234")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


async def test_criar_senha_uma_vez(
    app: FastAPI, client: httpx.AsyncClient, providers: ProvidersMock
):
    await flow(client, "google")

    weak = await client.post("/api/auth/set-password", json={"new_password": "fraca"})
    assert weak.status_code == 422

    created = await client.post("/api/auth/set-password", json={"new_password": "NovaSenha1"})
    assert created.status_code == 200
    assert created.json()["has_password"] is True
    assert created.json()["providers"] == ["google"]

    again = await client.post("/api/auth/set-password", json={"new_password": "OutraSenha2"})
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "password_already_set"

    async with extra_client(app) as other:
        assert (await login(other, "ana@exemplo.com", "NovaSenha1")).status_code == 200


async def test_criar_senha_exige_login(app: FastAPI):
    async with extra_client(app) as anonymous:
        response = await anonymous.post(
            "/api/auth/set-password", json={"new_password": "NovaSenha1"}
        )
    assert response.status_code == 401


async def test_trocar_email_e_senha_sem_senha_pede_criar_senha(
    client: httpx.AsyncClient, providers: ProvidersMock
):
    await flow(client, "google")

    email = await client.put(
        "/api/auth/me/email",
        json={"new_email": "nova@exemplo.com", "current_password": "qualquer"},
    )
    assert email.status_code == 400
    assert email.json()["error"] == {
        "code": "password_required",
        "message": "Crie uma senha antes de trocar o e-mail.",
        "fields": None,
    }

    password = await client.put(
        "/api/auth/me/password",
        json={"current_password": "qualquer", "new_password": "NovaSenha1"},
    )
    assert password.status_code == 400
    assert password.json()["error"]["code"] == "password_required"


async def test_excluir_conta_sem_senha_confirma_pelo_email(
    app: FastAPI, client: httpx.AsyncClient, providers: ProvidersMock
):
    await flow(client, "google")
    db = app.state.db
    user = await db[USERS].find_one({"email": "ana@exemplo.com"})
    await db[OAUTH_PENDING_LINKS].insert_one({"user_id": user["_id"], "token_hash": "x"})

    mismatch = await client.post(
        "/api/auth/delete-account", json={"confirm_email": "outra@exemplo.com"}
    )
    assert mismatch.status_code == 400
    assert mismatch.json()["error"]["code"] == "confirmation_mismatch"
    missing = await client.post("/api/auth/delete-account", json={})
    assert missing.json()["error"]["code"] == "confirmation_mismatch"

    response = await client.post(
        "/api/auth/delete-account", json={"confirm_email": "  Ana@Exemplo.com "}
    )
    assert response.status_code == 204
    assert await db[USERS].count_documents({}) == 0
    assert await db[OAUTH_PENDING_LINKS].count_documents({}) == 0


async def test_excluir_conta_com_senha_exige_a_senha(client: httpx.AsyncClient, app: FastAPI):
    await signup(client)

    only_email = await client.post(
        "/api/auth/delete-account", json={"confirm_email": "ana@exemplo.com"}
    )
    assert only_email.status_code == 400
    assert only_email.json()["error"]["code"] == "invalid_password"
    assert await app.state.db[USERS].count_documents({}) == 1

    ok = await client.post("/api/auth/delete-account", json={"current_password": "Senha1234"})
    assert ok.status_code == 204


async def test_usuario_com_senha_tem_campos_novos(client: httpx.AsyncClient):
    body = (await signup(client)).json()
    assert body["has_password"] is True
    assert body["providers"] == []

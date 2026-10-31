from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI

from app.core.config import settings
from app.core.security import DUMMY_PASSWORD_HASH, hasher, verify_password
from app.db.collections import DISPOSALS, FAVORITES, PASSWORD_RESETS, SESSIONS, USERS
from app.services import auth as auth_service

SIGNUP = {"name": "Ana Souza", "email": "ana@exemplo.com", "password": "Senha1234"}
OTHER_PASSWORD = "OutraSenha99"


@asynccontextmanager
async def extra_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Segundo cliente (outro 'navegador'), com cookies próprios."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-OleoApp-Client": "web"},
    ) as http_client:
        yield http_client


async def signup(client: httpx.AsyncClient, **overrides) -> httpx.Response:
    return await client.post("/api/auth/signup", json={**SIGNUP, **overrides})


async def login(client: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    return await client.post("/api/auth/login", json={"email": email, "password": password})


# --- cadastro ---------------------------------------------------------------


async def test_cadastro_devolve_usuario_sem_campos_de_senha_e_cria_cookie(
    client: httpx.AsyncClient,
):
    response = await signup(client)

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {
        "id",
        "name",
        "email",
        "bairro",
        "reminder_interval_days",
        "created_at",
        "has_password",
        "providers",
        "role",
    }
    assert body["name"] == "Ana Souza"
    assert body["email"] == "ana@exemplo.com"
    assert body["bairro"] is None
    assert body["reminder_interval_days"] == 180
    assert body["created_at"].endswith("Z")
    assert "password_hash" not in response.text
    assert "$argon2" not in response.text
    assert "token_hash" not in response.text
    assert client.cookies.get(settings.SESSION_COOKIE_NAME)


async def test_cookie_tem_httponly_samesite_lax_e_sem_secure_em_teste(
    client: httpx.AsyncClient,
):
    response = await signup(client)
    cookie = response.headers["set-cookie"].lower()

    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert f"max-age={settings.SESSION_TTL_DAYS * 86400}" in cookie
    assert "secure" not in cookie


async def test_cookie_tem_secure_quando_cookie_secure_true(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "COOKIE_SECURE", True)
    response = await signup(client)
    assert "secure" in response.headers["set-cookie"].lower()


async def test_cadastro_normaliza_email_para_minusculas(client: httpx.AsyncClient):
    response = await signup(client, email="  Ana@Exemplo.COM ")
    assert response.status_code == 201
    assert response.json()["email"] == "ana@exemplo.com"


async def test_cadastro_com_email_duplicado_responde_409(client: httpx.AsyncClient):
    assert (await signup(client)).status_code == 201

    response = await signup(client, email="ANA@exemplo.com")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_in_use"


@pytest.mark.parametrize(
    "password", ["senhafraca", "SenhaFraca", "Senha12", "senha1234", "a" * 129]
)
async def test_cadastro_com_senha_fraca_responde_422(client: httpx.AsyncClient, password: str):
    response = await signup(client, password=password)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["fields"][0]["field"] == "password"
    assert "A senha deve ter de 8 a 128 caracteres" in error["fields"][0]["message"]


async def test_cadastro_com_campo_extra_responde_422(client: httpx.AsyncClient):
    response = await signup(client, type="admin")

    assert response.status_code == 422
    fields = response.json()["error"]["fields"]
    assert any(item["field"] == "type" for item in fields)


async def test_cadastro_com_nome_curto_e_email_invalido_lista_os_campos(
    client: httpx.AsyncClient,
):
    response = await signup(client, name="A", email="nao-e-email")

    assert response.status_code == 422
    fields = {item["field"]: item["message"] for item in response.json()["error"]["fields"]}
    assert set(fields) == {"name", "email"}
    assert fields["email"] == "E-mail inválido."


async def test_hash_salvo_e_argon2id(app: FastAPI, client: httpx.AsyncClient):
    await signup(client)

    stored = await app.state.db[USERS].find_one({"email": "ana@exemplo.com"})
    assert stored["password_hash"].startswith("$argon2id")
    assert "Senha1234" not in stored["password_hash"]

    session = await app.state.db[SESSIONS].find_one({"user_id": stored["_id"]})
    assert len(session["token_hash"]) == 64  # SHA-256 em hexadecimal
    assert session["token_hash"] != client.cookies.get(settings.SESSION_COOKIE_NAME)


async def test_cadastro_tem_rate_limit_por_ip(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "RATE_LIMIT_SIGNUP_PER_HOUR", 2)

    assert (await signup(client, email="a1@exemplo.com")).status_code == 201
    assert (await signup(client, email="a2@exemplo.com")).status_code == 201
    blocked = await signup(client, email="a3@exemplo.com")

    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked.headers["retry-after"]) >= 1


# --- login ------------------------------------------------------------------


async def test_login_certo_e_errado(app: FastAPI, client: httpx.AsyncClient):
    await signup(client)

    async with extra_client(app) as other:
        ok = await login(other, "ana@exemplo.com", "Senha1234")
        assert ok.status_code == 200
        assert ok.json()["email"] == "ana@exemplo.com"
        assert other.cookies.get(settings.SESSION_COOKIE_NAME)

    wrong_password = await login(client, "ana@exemplo.com", "SenhaErrada1")
    unknown_email = await login(client, "ninguem@exemplo.com", "Senha1234")

    for response in (wrong_password, unknown_email):
        assert response.status_code == 401
        assert response.json()["error"] == {
            "code": "invalid_credentials",
            "message": "E-mail ou senha incorretos.",
            "fields": None,
        }


async def test_login_com_email_inexistente_roda_o_argon2_contra_o_hash_ficticio(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    calls: list[str] = []

    def spy(password: str, password_hash: str) -> bool:
        calls.append(password_hash)
        return verify_password(password, password_hash)

    monkeypatch.setattr(auth_service, "verify_password", spy)

    response = await login(client, "ninguem@exemplo.com", "Senha1234")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    # Mesmo sem usuário, a senha passa pelo Argon2 (mesmos parâmetros do hash real).
    assert calls == [DUMMY_PASSWORD_HASH]
    assert hasher.check_needs_rehash(DUMMY_PASSWORD_HASH) is False


def test_hash_ficticio_nunca_aceita_senha():
    assert verify_password("", DUMMY_PASSWORD_HASH) is False
    assert verify_password("Senha1234", DUMMY_PASSWORD_HASH) is False


async def test_sexto_login_no_mesmo_minuto_responde_429(client: httpx.AsyncClient):
    await signup(client)

    for _ in range(5):
        response = await login(client, "ana@exemplo.com", "SenhaErrada1")
        assert response.status_code == 401

    blocked = await login(client, "ana@exemplo.com", "SenhaErrada1")
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked.headers["retry-after"]) >= 1

    # O limite é por IP + e-mail: outro e-mail não é afetado.
    other_email = await login(client, "outra@exemplo.com", "SenhaErrada1")
    assert other_email.status_code == 401


# --- sessão -----------------------------------------------------------------


async def test_me_com_e_sem_cookie(app: FastAPI, client: httpx.AsyncClient):
    async with extra_client(app) as anonymous:
        response = await anonymous.get("/api/auth/me")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"

    await signup(client)
    response = await client.get("/api/auth/me")
    assert response.status_code == 200
    assert response.json()["email"] == "ana@exemplo.com"
    assert "password_hash" not in response.text


async def test_cookie_invalido_responde_401(client: httpx.AsyncClient):
    client.cookies.set(settings.SESSION_COOKIE_NAME, "token-que-nao-existe")
    response = await client.get("/api/auth/me")
    assert response.status_code == 401


async def test_sessao_expirada_responde_401(app: FastAPI, client: httpx.AsyncClient):
    await signup(client)
    past = datetime.now(UTC) - timedelta(minutes=1)
    await app.state.db[SESSIONS].update_many({}, {"$set": {"expires_at": past}})

    response = await client.get("/api/auth/me")
    assert response.status_code == 401


async def test_logout_invalida_a_sessao_e_limpa_o_cookie(app: FastAPI, client: httpx.AsyncClient):
    await signup(client)
    token = client.cookies.get(settings.SESSION_COOKIE_NAME)

    response = await client.post("/api/auth/logout")
    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert await app.state.db[SESSIONS].count_documents({}) == 0

    # Mesmo reenviando o cookie antigo, a sessão não vale mais.
    client.cookies.set(settings.SESSION_COOKIE_NAME, token)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_logout_sem_sessao_responde_204_e_limpa_o_cookie(
    app: FastAPI, client: httpx.AsyncClient
):
    async with extra_client(app) as anonymous:
        response = await anonymous.post("/api/auth/logout")
    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()


# --- perfil -----------------------------------------------------------------


async def test_patch_me_atualiza_nome_bairro_e_intervalo(client: httpx.AsyncClient):
    await signup(client)

    response = await client.patch(
        "/api/auth/me",
        json={"name": " Ana Maria ", "bairro": "Centro", "reminder_interval_days": 90},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["name"] == "Ana Maria"
    assert body["bairro"] == "Centro"
    assert body["reminder_interval_days"] == 90

    assert (await client.get("/api/auth/me")).json() == body


async def test_patch_me_limpa_o_bairro_com_null_ou_texto_vazio(client: httpx.AsyncClient):
    await signup(client)
    await client.patch("/api/auth/me", json={"bairro": "Centro"})

    cleared = await client.patch("/api/auth/me", json={"bairro": None})
    assert cleared.status_code == 200
    assert cleared.json()["bairro"] is None

    await client.patch("/api/auth/me", json={"bairro": "Itaipuaçu"})
    blank = await client.patch("/api/auth/me", json={"bairro": "   "})
    assert blank.json()["bairro"] is None


@pytest.mark.parametrize(
    "payload",
    [
        {"reminder_interval_days": 100},
        {},
        {"name": None},
        {"reminder_interval_days": None},
        {"name": "A"},
        {"bairro": "x" * 61},
        {"email": "outro@exemplo.com"},
    ],
)
async def test_patch_me_invalido_responde_422(client: httpx.AsyncClient, payload: dict):
    await signup(client)
    response = await client.patch("/api/auth/me", json=payload)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_rotas_de_conta_exigem_login(app: FastAPI, client: httpx.AsyncClient):
    async with extra_client(app) as anonymous:
        calls = [
            await anonymous.patch("/api/auth/me", json={"name": "Ana"}),
            await anonymous.put(
                "/api/auth/me/email",
                json={"new_email": "x@exemplo.com", "current_password": "Senha1234"},
            ),
            await anonymous.put(
                "/api/auth/me/password",
                json={"current_password": "Senha1234", "new_password": OTHER_PASSWORD},
            ),
            await anonymous.post("/api/auth/delete-account", json={"current_password": "x"}),
        ]
    assert [call.status_code for call in calls] == [401, 401, 401, 401]


# --- troca de e-mail --------------------------------------------------------


async def test_troca_de_email(client: httpx.AsyncClient):
    await signup(client)

    wrong = await client.put(
        "/api/auth/me/email",
        json={"new_email": "nova@exemplo.com", "current_password": "SenhaErrada1"},
    )
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "invalid_password"

    ok = await client.put(
        "/api/auth/me/email",
        json={"new_email": "Nova@Exemplo.com", "current_password": "Senha1234"},
    )
    assert ok.status_code == 200
    assert ok.json()["email"] == "nova@exemplo.com"

    assert (await login(client, "nova@exemplo.com", "Senha1234")).status_code == 200
    assert (await login(client, "ana@exemplo.com", "Senha1234")).status_code == 401


async def test_troca_de_email_para_um_em_uso_responde_409(app: FastAPI, client: httpx.AsyncClient):
    async with extra_client(app) as other:
        await signup(other, name="Bruno Lima", email="bruno@exemplo.com")

    await signup(client)
    response = await client.put(
        "/api/auth/me/email",
        json={"new_email": "bruno@exemplo.com", "current_password": "Senha1234"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_in_use"


# --- troca de senha ---------------------------------------------------------


async def test_troca_de_senha_derruba_a_outra_sessao_e_mantem_a_atual(
    app: FastAPI, client: httpx.AsyncClient
):
    await signup(client)

    async with extra_client(app) as other:
        assert (await login(other, "ana@exemplo.com", "Senha1234")).status_code == 200
        assert (await other.get("/api/auth/me")).status_code == 200

        wrong = await client.put(
            "/api/auth/me/password",
            json={"current_password": "SenhaErrada1", "new_password": OTHER_PASSWORD},
        )
        assert wrong.status_code == 400
        assert wrong.json()["error"]["code"] == "invalid_password"

        changed = await client.put(
            "/api/auth/me/password",
            json={"current_password": "Senha1234", "new_password": OTHER_PASSWORD},
        )
        assert changed.status_code == 204

        assert (await other.get("/api/auth/me")).status_code == 401
        assert (await client.get("/api/auth/me")).status_code == 200

    assert (await login(client, "ana@exemplo.com", "Senha1234")).status_code == 401
    assert (await login(client, "ana@exemplo.com", OTHER_PASSWORD)).status_code == 200


async def test_nova_senha_fraca_ou_igual_a_atual_responde_422(client: httpx.AsyncClient):
    await signup(client)

    weak = await client.put(
        "/api/auth/me/password",
        json={"current_password": "Senha1234", "new_password": "fraca"},
    )
    assert weak.status_code == 422
    assert weak.json()["error"]["fields"][0]["field"] == "new_password"

    same = await client.put(
        "/api/auth/me/password",
        json={"current_password": "Senha1234", "new_password": "Senha1234"},
    )
    assert same.status_code == 422


# --- exclusão de conta ------------------------------------------------------


async def test_exclusao_de_conta_apaga_tudo_do_usuario(app: FastAPI, client: httpx.AsyncClient):
    db = app.state.db
    await signup(client)
    async with extra_client(app) as bystander:
        await signup(bystander, name="Bruno Lima", email="bruno@exemplo.com")
    user_id = (await db[USERS].find_one({"email": "ana@exemplo.com"}))["_id"]
    other_id = (await db[USERS].find_one({"email": "bruno@exemplo.com"}))["_id"]

    now = datetime.now(UTC)
    for collection in (PASSWORD_RESETS, DISPOSALS, FAVORITES):
        await db[collection].insert_many(
            [
                {"user_id": user_id, "token_hash": f"{collection}-ana", "created_at": now},
                {"user_id": other_id, "token_hash": f"{collection}-bruno", "created_at": now},
            ]
        )

    wrong = await client.post("/api/auth/delete-account", json={"current_password": "Errada1x"})
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "invalid_password"
    assert await db[USERS].count_documents({"_id": user_id}) == 1

    response = await client.post("/api/auth/delete-account", json={"current_password": "Senha1234"})
    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()

    assert await db[USERS].count_documents({"_id": user_id}) == 0
    assert await db[SESSIONS].count_documents({"user_id": user_id}) == 0
    for collection in (PASSWORD_RESETS, DISPOSALS, FAVORITES):
        assert await db[collection].count_documents({"user_id": user_id}) == 0
        # Os dados de outras pessoas não são tocados.
        assert await db[collection].count_documents({"user_id": other_id}) == 1
    assert await db[USERS].count_documents({"_id": other_id}) == 1

    assert (await client.get("/api/auth/me")).status_code == 401
    assert (await login(client, "ana@exemplo.com", "Senha1234")).status_code == 401


async def test_sessao_de_usuario_removido_responde_401(app: FastAPI, client: httpx.AsyncClient):
    await signup(client)
    await app.state.db[USERS].delete_many({})

    assert (await client.get("/api/auth/me")).status_code == 401

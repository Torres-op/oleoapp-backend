from datetime import UTC, datetime, timedelta

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.core.config import settings
from app.db.collections import AUDIT_LOGS, DISPOSALS, USERS
from app.services import admin as admin_service
from tests.factories import insert_disposal
from tests.test_auth import extra_client, signup

ADMIN_PATHS = [
    "/api/admin/metrics/overview",
    "/api/admin/metrics/timeseries",
    "/api/admin/users",
    "/api/admin/logs",
    "/api/admin/system/status",
]


async def make_admin(app: FastAPI, client: httpx.AsyncClient, **overrides) -> str:
    """Cadastra uma conta de verdade e marca role=admin direto no banco."""
    body = (await signup(client, **overrides)).json()
    await app.state.db[USERS].update_one({"_id": ObjectId(body["id"])}, {"$set": {"role": "admin"}})
    return body["id"]


async def insert_user(app: FastAPI, email: str, **fields) -> ObjectId:
    now = datetime.now(UTC)
    document = {
        "name": fields.pop("name", email.split("@")[0]),
        "email": email,
        "password_hash": "x",
        "bairro": None,
        "reminder_interval_days": 180,
        "created_at": now,
        "updated_at": now,
        **fields,
    }
    result = await app.state.db[USERS].insert_one(document)
    return result.inserted_id


async def insert_event(app: FastAPI, event_type: str, **fields) -> None:
    await app.state.db[AUDIT_LOGS].insert_one(
        {
            "created_at": fields.pop("created_at", datetime.now(UTC)),
            "type": event_type,
            "user_id": fields.pop("user_id", None),
            "email": fields.pop("email", None),
            "ip": "1.2.3.4",
            "user_agent": "teste",
            "success": fields.pop("success", True),
            "reason": None,
            "method": None,
            **fields,
        }
    )


# --- acesso ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ADMIN_PATHS)
async def test_sem_login_responde_401(client: httpx.AsyncClient, path: str):
    response = await client.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


@pytest.mark.parametrize("path", ADMIN_PATHS)
async def test_usuario_comum_responde_403(client: httpx.AsyncClient, path: str):
    await signup(client)
    response = await client.get(path)
    assert response.status_code == 403
    assert response.json()["error"] == {
        "code": "forbidden",
        "message": "Acesso restrito à administração.",
        "fields": None,
    }


@pytest.mark.parametrize("path", ADMIN_PATHS)
async def test_admin_responde_200(app: FastAPI, client: httpx.AsyncClient, path: str):
    await make_admin(app, client)
    assert (await client.get(path)).status_code == 200


async def test_papel_vem_do_banco_e_nao_do_cliente(app: FastAPI, client: httpx.AsyncClient):
    await signup(client)
    # O perfil não aceita "role": quem decide o papel é só o banco (via script).
    assert (await client.patch("/api/auth/me", json={"role": "admin"})).status_code == 422
    assert (await client.get("/api/auth/me")).json()["role"] == "user"
    assert (await client.get("/api/admin/users")).status_code == 403


async def test_me_mostra_o_papel_de_admin(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    assert (await client.get("/api/auth/me")).json()["role"] == "admin"


async def test_rate_limit_do_admin(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    if app.state.redis is None:
        pytest.skip("Sem Redis não há rate limit.")
    await make_admin(app, client)
    monkeypatch.setattr(settings, "RATE_LIMIT_ADMIN_PER_MIN", 1)
    assert (await client.get("/api/admin/system/status")).status_code == 200
    blocked = await client.get("/api/admin/system/status")
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"


@pytest.mark.parametrize(
    "path",
    [
        "/api/admin/users?page_size=101",
        "/api/admin/users?page=0",
        "/api/admin/users?status=banido",
        "/api/admin/logs?page_size=101",
        "/api/admin/logs?type=invasao",
        "/api/admin/logs?user_id=nao-e-id",
        "/api/admin/logs?from=31-12-2026",
        "/api/admin/metrics/timeseries?days=15",
        "/api/admin/metrics/timeseries?days=abc",
    ],
)
async def test_parametros_invalidos_respondem_422(
    app: FastAPI, client: httpx.AsyncClient, path: str
):
    await make_admin(app, client)
    response = await client.get(path)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --- métricas ---------------------------------------------------------------------------


async def test_overview_conta_usuarios_logins_descartes_e_impacto(
    app: FastAPI, client: httpx.AsyncClient
):
    admin_id = await make_admin(app, client)
    now = datetime.now(UTC)
    old_id = await insert_user(app, "antigo@exemplo.com", created_at=now - timedelta(days=40))
    await insert_user(app, "ativo@exemplo.com", last_login_at=now - timedelta(days=2))

    await insert_disposal(app, admin_id, volume_ml=2000)
    await insert_disposal(app, str(old_id), volume_ml=1000, age=timedelta(days=60))
    point_id = ObjectId()
    for _ in range(2):
        await app.state.db[DISPOSALS].insert_one(
            {
                "user_id": old_id,
                "point_id": point_id,
                "point_name": "Posto Central",
                "volume_ml": 500,
                "created_at": now,
            }
        )

    await insert_event(app, "login_success")
    await insert_event(app, "login_failure", success=False)
    await insert_event(app, "login_failure", success=False, created_at=now - timedelta(days=3))
    await insert_event(app, "account_deleted")

    body = (await client.get("/api/admin/metrics/overview")).json()

    assert body["users"]["total"] == 3
    assert body["users"]["new_30d"] == 2
    assert body["users"]["admins"] == 1
    assert body["users"]["active_30d"] == 1
    assert body["logins"]["failed_7d"] == 2
    assert body["logins"]["success_7d"] >= 1
    assert body["accounts_deleted_30d"] == 1
    assert body["disposals"] == {
        "total": 4,
        "total_liters": 4.0,
        "last_30d": 3,
        "liters_30d": 3.0,
    }
    assert body["impact"] == {"water_l": 4_000_000.0, "co2e_kg": 4.0}
    assert body["top_points"] == [
        {"id": str(point_id), "name": "Posto Central", "disposals": 2, "liters": 1.0}
    ]


async def test_timeseries_preenche_todos_os_dias(app: FastAPI, client: httpx.AsyncClient):
    admin_id = await make_admin(app, client)
    await insert_disposal(app, admin_id, volume_ml=1500)
    await insert_event(app, "login_failure", success=False)

    body = (await client.get("/api/admin/metrics/timeseries?days=7")).json()

    assert body["days"] == 7
    dates = [day["date"] for day in body["series"]]
    assert len(dates) == 7
    assert dates == sorted(dates)
    assert dates[-1] == admin_service.today_local().isoformat()
    today = body["series"][-1]
    assert today["signups"] == 1
    assert today["disposals"] == 1
    assert today["liters"] == 1.5
    assert today["login_failures"] == 1
    assert sum(day["disposals"] for day in body["series"][:-1]) == 0


# --- usuários ---------------------------------------------------------------------------


async def test_lista_de_usuarios_busca_filtra_e_pagina(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    now = datetime.now(UTC)
    bruno = await insert_user(app, "bruno@exemplo.com", name="Bruno Lima", last_login_at=now)
    await insert_user(app, "carla@exemplo.com", name="Carla", password_hash=None)
    await insert_disposal(app, str(bruno), volume_ml=1000)
    await insert_disposal(app, str(bruno), volume_ml=1000)

    found = (await client.get("/api/admin/users", params={"q": "BRUNO"})).json()
    assert found["total"] == 1
    item = found["items"][0]
    assert item["email"] == "bruno@exemplo.com"
    assert item["disposals_count"] == 2
    assert item["has_password"] is True
    assert set(item) == {
        "id",
        "name",
        "email",
        "role",
        "bairro",
        "created_at",
        "last_login_at",
        "has_password",
        "providers",
        "disposals_count",
        "disabled",
        "disabled_at",
    }
    assert item["disabled"] is False

    no_password = (await client.get("/api/admin/users?status=no_password")).json()
    assert [u["email"] for u in no_password["items"]] == ["carla@exemplo.com"]
    admins = (await client.get("/api/admin/users?status=admin")).json()
    assert [u["role"] for u in admins["items"]] == ["admin"]
    # Ativo = login nos últimos 30 dias (o cadastro não conta como login).
    active = (await client.get("/api/admin/users?status=active")).json()
    assert [u["email"] for u in active["items"]] == ["bruno@exemplo.com"]
    inactive = (await client.get("/api/admin/users?status=inactive")).json()
    assert {u["email"] for u in inactive["items"]} == {"carla@exemplo.com", "ana@exemplo.com"}

    page = (await client.get("/api/admin/users?page=2&page_size=2")).json()
    assert page["total"] == 3
    assert len(page["items"]) == 1
    assert "password_hash" not in (await client.get("/api/admin/users")).text


async def test_busca_trata_caracteres_especiais_como_texto(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    await insert_user(app, "x@exemplo.com", name="Teste")
    body = (await client.get("/api/admin/users", params={"q": ".*"})).json()
    assert body["total"] == 0


# --- logs ----------------------------------------------------------------------------------


async def test_logs_filtram_por_tipo_sucesso_usuario_e_periodo(
    app: FastAPI, client: httpx.AsyncClient
):
    await make_admin(app, client)
    target = ObjectId()
    old = datetime.now(UTC) - timedelta(days=10)
    await insert_event(app, "login_failure", success=False, email="b***@exemplo.com")
    await insert_event(app, "disposal_created", user_id=target)
    await insert_event(app, "signup", user_id=target, created_at=old)

    failures = (await client.get("/api/admin/logs?type=login_failure&success=false")).json()
    assert failures["total"] == 1
    assert failures["items"][0]["email"] == "b***@exemplo.com"

    by_user = (await client.get(f"/api/admin/logs?user_id={target}")).json()
    assert [item["type"] for item in by_user["items"]] == ["disposal_created", "signup"]

    since = (datetime.now(UTC) - timedelta(days=2)).date().isoformat()
    recent = (await client.get(f"/api/admin/logs?user_id={target}&from={since}")).json()
    assert [item["type"] for item in recent["items"]] == ["disposal_created"]
    until = (old + timedelta(days=1)).date().isoformat()
    older = (await client.get(f"/api/admin/logs?user_id={target}&to={until}")).json()
    assert [item["type"] for item in older["items"]] == ["signup"]


async def test_admin_access_so_em_usuarios_e_logs(app: FastAPI, client: httpx.AsyncClient):
    admin_id = await make_admin(app, client)
    for path in ADMIN_PATHS:
        await client.get(path)

    events = await app.state.db[AUDIT_LOGS].find({"type": "admin_access"}).to_list(length=None)
    assert sorted(event["reason"] for event in events) == ["/api/admin/logs", "/api/admin/users"]
    assert all(event["user_id"] == ObjectId(admin_id) for event in events)


# --- status do sistema ------------------------------------------------------------------------


async def test_status_mostra_api_banco_e_integracoes_sem_segredos(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", "chave-super-secreta")
    await make_admin(app, client)

    response = await client.get("/api/admin/system/status")
    body = response.json()

    assert body["api"]["env"] == "test"
    assert body["api"]["version"]
    assert body["api"]["uptime_s"] >= 0
    assert body["database"]["ok"] is True
    assert body["database"]["latency_ms"] is not None
    names = {c["name"] for c in body["database"]["collections"]}
    assert {"users", "disposals", "audit_logs"} <= names
    assert body["integrations"]["google_maps"] is True
    assert body["integrations"]["email_provider"] == "console"
    assert "chave-super-secreta" not in response.text


async def test_status_com_banco_fora_do_ar_nao_quebra(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    await make_admin(app, client)
    db = app.state.db

    original = type(db).command

    async def failing_command(self, command, *args, **kwargs):
        if command == "ping":
            raise RuntimeError("fora do ar")
        return await original(self, command, *args, **kwargs)

    monkeypatch.setattr(type(db), "command", failing_command)
    body = (await client.get("/api/admin/system/status")).json()
    assert body["database"] == {"ok": False, "latency_ms": None, "collections": []}


async def test_lista_nao_expoe_hash_nem_login_social(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    async with extra_client(app) as other:
        await signup(other, name="Bruno Lima", email="bruno@exemplo.com")
    text = (await client.get("/api/admin/users")).text
    assert "$argon2" not in text and "oauth" not in text

import csv
import io
import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.core.config import settings
from app.db.collections import AUDIT_LOGS, OAUTH_PENDING_LINKS, POINTS, SESSIONS, USERS
from app.services import admin as admin_service
from scripts.import_points import run, safe_target
from tests.factories import CENTER_LAT, CENTER_LNG, insert_point, point_seed_data
from tests.test_admin import insert_event, make_admin
from tests.test_auth import SIGNUP, extra_client, login, signup
from tests.test_oauth import FRONT, GOOGLE_SECRET, ProvidersMock, flow, location

ADMIN = {"name": "Admin Teste", "email": "admin@exemplo.com"}
OID = str(ObjectId())
POINT_BODY = {
    "name": "Oficina do Admin",
    "address": {"street": "Rua Abreu Rangel", "number": "120", "bairro": "Centro"},
    "location": {"lat": CENTER_LAT, "lng": CENTER_LNG},
    "accepted_oils": ["motor_carro"],
}
NEW_ROUTES = [
    ("GET", "/api/admin/alerts", None),
    ("GET", "/api/admin/logs/export", None),
    ("GET", "/api/admin/points", None),
    ("GET", f"/api/admin/points/{OID}", None),
    ("POST", "/api/admin/points", POINT_BODY),
    ("PATCH", f"/api/admin/points/{OID}", {"name": "Novo nome"}),
    ("POST", f"/api/admin/users/{OID}/deactivate", {"reason": None}),
    ("POST", f"/api/admin/users/{OID}/reactivate", None),
]


async def admin_client_ctx(app: FastAPI):
    """Segundo navegador já logado como admin (o `client` fica para o usuário comum)."""
    ctx = extra_client(app)
    other = await ctx.__aenter__()
    admin_id = await make_admin(app, other, **ADMIN)
    return ctx, other, admin_id


async def call(client: httpx.AsyncClient, method: str, path: str, body: dict | None):
    return await client.request(method, path, json=body)


async def events(app: FastAPI, **query) -> list[dict]:
    return await app.state.db[AUDIT_LOGS].find(query).sort("_id", 1).to_list(length=None)


# --- acesso -------------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path", "body"), NEW_ROUTES)
async def test_rotas_novas_sem_login_401(client: httpx.AsyncClient, method, path, body):
    response = await call(client, method, path, body)
    assert response.status_code == 401


@pytest.mark.parametrize(("method", "path", "body"), NEW_ROUTES)
async def test_rotas_novas_usuario_comum_403(client: httpx.AsyncClient, method, path, body):
    await signup(client)
    response = await call(client, method, path, body)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_rotas_de_leitura_respondem_ao_admin(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    for path in ("/api/admin/alerts", "/api/admin/logs/export", "/api/admin/points"):
        assert (await client.get(path)).status_code == 200, path


async def test_escrita_exige_o_cabecalho_do_app(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as bare:
        bare.cookies = client.cookies
        response = await bare.post("/api/admin/points", json=POINT_BODY)
    assert response.status_code == 403
    assert await app.state.db[POINTS].count_documents({}) == 0


# --- desativar e reativar contas ------------------------------------------------------------


async def test_desativar_derruba_sessoes_e_bloqueia_o_login(
    app: FastAPI, client: httpx.AsyncClient
):
    ana_id = (await signup(client)).json()["id"]
    ctx, admin, admin_id = await admin_client_ctx(app)
    try:
        response = await admin.post(
            f"/api/admin/users/{ana_id}/deactivate", json={"reason": "Spam no app"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == ana_id
        assert body["disabled"] is True
        assert body["disabled_at"] is not None

        # Sessões apagadas: a Ana sai na hora.
        assert await app.state.db[SESSIONS].count_documents({"user_id": ObjectId(ana_id)}) == 0
        assert (await client.get("/api/auth/me")).status_code == 401

        # Senha certa: 403 account_disabled. Senha errada: o erro de sempre.
        blocked = await login(client, SIGNUP["email"], SIGNUP["password"])
        assert blocked.status_code == 403
        assert blocked.json()["error"] == {
            "code": "account_disabled",
            "message": "Esta conta foi desativada. Fale com o suporte.",
            "fields": None,
        }
        wrong = await login(client, SIGNUP["email"], "SenhaErrada1")
        assert wrong.status_code == 401
        assert wrong.json()["error"]["code"] == "invalid_credentials"

        (event,) = await events(app, type="admin_user_deactivated")
        assert event["user_id"] == ObjectId(admin_id)
        assert event["target_id"] == ana_id
        assert event["reason"] == "Spam no app"
        failures = await events(app, type="login_failure", reason="account_disabled")
        assert len(failures) == 1

        listed = (await admin.get("/api/admin/users?status=disabled")).json()
        assert [u["id"] for u in listed["items"]] == [ana_id]
        log_item = (await admin.get("/api/admin/logs?type=admin_user_deactivated")).json()
        assert log_item["items"][0]["target_id"] == ana_id
    finally:
        await ctx.__aexit__(None, None, None)


async def test_desativar_e_idempotente_e_reativar_volta_a_entrar(
    app: FastAPI, client: httpx.AsyncClient
):
    ana_id = (await signup(client)).json()["id"]
    ctx, admin, _ = await admin_client_ctx(app)
    try:
        for _ in range(2):
            response = await admin.post(f"/api/admin/users/{ana_id}/deactivate", json={})
            assert response.status_code == 200
        assert len(await events(app, type="admin_user_deactivated")) == 1

        reactivated = await admin.post(f"/api/admin/users/{ana_id}/reactivate")
        assert reactivated.status_code == 200
        assert reactivated.json()["disabled"] is False
        assert reactivated.json()["disabled_at"] is None
        assert (await admin.post(f"/api/admin/users/{ana_id}/reactivate")).status_code == 200
        assert len(await events(app, type="admin_user_reactivated")) == 1

        assert (await login(client, SIGNUP["email"], SIGNUP["password"])).status_code == 200
        user = await app.state.db[USERS].find_one({"_id": ObjectId(ana_id)})
        assert user["disabled_at"] is None and user["disabled_reason"] is None
    finally:
        await ctx.__aexit__(None, None, None)


async def test_desativar_apaga_vinculos_pendentes(app: FastAPI, client: httpx.AsyncClient):
    ana_id = (await signup(client)).json()["id"]
    await app.state.db[OAUTH_PENDING_LINKS].insert_one(
        {"user_id": ObjectId(ana_id), "token_hash": "x", "expires_at": datetime.now(UTC)}
    )
    ctx, admin, _ = await admin_client_ctx(app)
    try:
        await admin.post(f"/api/admin/users/{ana_id}/deactivate", json={})
    finally:
        await ctx.__aexit__(None, None, None)
    assert await app.state.db[OAUTH_PENDING_LINKS].count_documents({}) == 0


async def test_nao_desativa_a_si_mesmo_nem_outro_admin(app: FastAPI, client: httpx.AsyncClient):
    admin_id = await make_admin(app, client, **ADMIN)
    me = await client.post(f"/api/admin/users/{admin_id}/deactivate", json={})
    assert me.status_code == 400
    assert me.json()["error"]["code"] == "cannot_deactivate_self"

    async with extra_client(app) as other:
        other_admin = await make_admin(app, other)
    response = await client.post(f"/api/admin/users/{other_admin}/deactivate", json={})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "cannot_deactivate_admin"


@pytest.mark.parametrize("user_id", ["nao-e-id", OID])
async def test_desativar_usuario_inexistente_404(
    app: FastAPI, client: httpx.AsyncClient, user_id: str
):
    await make_admin(app, client)
    for action in ("deactivate", "reactivate"):
        response = await client.post(f"/api/admin/users/{user_id}/{action}", json={})
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


async def test_motivo_longo_demais_422(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    response = await client.post(f"/api/admin/users/{OID}/deactivate", json={"reason": "x" * 201})
    assert response.status_code == 422


async def test_google_em_conta_desativada_volta_ao_login(
    app: FastAPI,
    client: httpx.AsyncClient,
    http_mock: Callable,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "FRONTEND_URL", FRONT)
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "id-google")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", GOOGLE_SECRET)
    providers = ProvidersMock()  # perfil com o mesmo e-mail da Ana, verificado
    http_mock(providers.handler)

    ana_id = (await signup(client)).json()["id"]
    ctx, admin, _ = await admin_client_ctx(app)
    try:
        await admin.post(f"/api/admin/users/{ana_id}/deactivate", json={})
    finally:
        await ctx.__aexit__(None, None, None)

    async with extra_client(app) as browser:
        response = await flow(browser, "google")
        assert location(response) == f"{FRONT}/login?erro=conta_desativada"
        assert not browser.cookies.get(settings.SESSION_COOKIE_NAME)

    user = await app.state.db[USERS].find_one({"_id": ObjectId(ana_id)})
    assert "oauth" not in user  # não vincula o Google a uma conta desativada
    (failure,) = await events(app, type="login_failure", method="google")
    assert failure["reason"] == "account_disabled"


# --- pontos de coleta -------------------------------------------------------------------------


async def test_criar_ponto_aparece_no_app(app: FastAPI, client: httpx.AsyncClient):
    admin_id = await make_admin(app, client)
    response = await client.post("/api/admin/points", json=POINT_BODY)

    assert response.status_code == 201
    point = response.json()
    assert re.fullmatch(r"admin-oficina-do-admin-[0-9a-f]{6}", point["slug"])
    assert point["managed_by"] == "admin"
    assert point["source"] == "admin"
    assert point["active"] is True
    assert point["verified"] is False and point["verified_at"] is None
    assert point["address"]["city"] == "Maricá" and point["address"]["state"] == "RJ"
    assert point["address"]["formatted"].startswith("Rua Abreu Rangel, 120 - Centro")
    assert point["location"] == {"lat": CENTER_LAT, "lng": CENTER_LNG}

    near = await client.get(
        "/api/points/near", params={"lat": CENTER_LAT, "lng": CENTER_LNG, "radius_m": 1000}
    )
    assert [p["id"] for p in near.json()["items"]] == [point["id"]]

    (event,) = await events(app, type="admin_point_created")
    assert event["user_id"] == ObjectId(admin_id)
    assert event["target_id"] == point["id"]
    got = await client.get(f"/api/admin/points/{point['id']}")
    assert got.status_code == 200 and got.json()["slug"] == point["slug"]


async def test_criar_ponto_verificado_grava_a_data(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    point = (await client.post("/api/admin/points", json={**POINT_BODY, "verified": True})).json()
    assert point["verified"] is True
    assert point["verified_at"].startswith(admin_service.today_local().isoformat())


@pytest.mark.parametrize(
    "override",
    [
        {"location": {"lat": -23.5505, "lng": -46.6333}},  # São Paulo: fora da área
        {"address": {"street": "Rua X", "bairro": "Centro", "cep": "24900000"}},
        {"address": {"street": "Rua X", "bairro": "Centro", "state": "RJX"}},
        {"accepted_oils": ["motor_carro", "motor_carro"]},
        {"accepted_oils": []},
        {"name": "X"},
        {"source": "invasor"},
    ],
)
async def test_criar_ponto_invalido_422(app: FastAPI, client: httpx.AsyncClient, override: dict):
    await make_admin(app, client)
    response = await client.post("/api/admin/points", json={**POINT_BODY, **override})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert await app.state.db[POINTS].count_documents({}) == 0


async def test_editar_ponto_parcial_e_verificacao(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    point = (await client.post("/api/admin/points", json=POINT_BODY)).json()
    url = f"/api/admin/points/{point['id']}"

    renamed = (await client.patch(url, json={"name": "Oficina Nova", "phone": "21 9999"})).json()
    assert renamed["name"] == "Oficina Nova"
    assert renamed["phone"] == "21 9999"
    assert renamed["address"] == point["address"]  # o resto fica como estava

    verified = (await client.patch(url, json={"verified": True})).json()
    assert verified["verified_at"] is not None
    again = (await client.patch(url, json={"verified": True})).json()
    assert again["verified_at"] == verified["verified_at"]
    unverified = (await client.patch(url, json={"verified": False, "phone": None})).json()
    assert unverified["verified_at"] is None
    assert unverified["phone"] is None

    reasons = [e["reason"] for e in await events(app, type="admin_point_updated")]
    assert reasons == ["name,phone", "verified", "verified", "phone,verified"]
    assert all(e["target_id"] == point["id"] for e in await events(app, type="admin_point_updated"))


async def test_desativar_ponto_some_do_app(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    point = (await client.post("/api/admin/points", json=POINT_BODY)).json()
    inactive = await client.patch(f"/api/admin/points/{point['id']}", json={"active": False})
    assert inactive.json()["active"] is False

    near = await client.get(
        "/api/points/near", params={"lat": CENTER_LAT, "lng": CENTER_LNG, "radius_m": 1000}
    )
    assert near.json()["items"] == []
    assert (await client.get(f"/api/points/{point['id']}")).status_code == 404
    listed = (await client.get("/api/admin/points?status=inactive")).json()
    assert [p["id"] for p in listed["items"]] == [point["id"]]


async def test_editar_ponto_importado_marca_managed_by(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    point_id = await insert_point(app, "Posto do Seed", slug="posto-do-seed")
    response = await client.patch(
        f"/api/admin/points/{point_id}",
        json={"address": {"street": "Rua Nova", "bairro": "Flamengo", "cep": "24900-000"}},
    )
    body = response.json()
    assert response.status_code == 200
    assert body["managed_by"] == "admin"
    assert body["slug"] == "posto-do-seed"
    assert body["address"]["bairro"] == "Flamengo"
    assert body["address"]["number"] is None


@pytest.mark.parametrize(
    "body",
    [
        {"name": None},
        {"location": {"lat": -23.5505, "lng": -46.6333}},
        {"accepted_oils": None},
        {"active": None},
    ],
)
async def test_editar_ponto_invalido_422(app: FastAPI, client: httpx.AsyncClient, body: dict):
    await make_admin(app, client)
    point = (await client.post("/api/admin/points", json=POINT_BODY)).json()
    response = await client.patch(f"/api/admin/points/{point['id']}", json=body)
    assert response.status_code == 422


@pytest.mark.parametrize("point_id", ["nao-e-id", OID])
async def test_ponto_inexistente_404(app: FastAPI, client: httpx.AsyncClient, point_id: str):
    await make_admin(app, client)
    assert (await client.get(f"/api/admin/points/{point_id}")).status_code == 404
    patched = await client.patch(f"/api/admin/points/{point_id}", json={"name": "Novo"})
    assert patched.status_code == 404


async def test_lista_de_pontos_busca_filtra_e_pagina(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    await insert_point(app, "Borracharia Zeta", slug="zeta")
    await insert_point(app, "Auto Center Alfa", slug="alfa")
    await insert_point(app, "Posto Inativo", slug="inativo", active=False)

    page = (await client.get("/api/admin/points?page_size=2")).json()
    assert page["total"] == 3
    assert [p["name"] for p in page["items"]] == ["Auto Center Alfa", "Borracharia Zeta"]

    found = (await client.get("/api/admin/points", params={"q": "zeta"})).json()
    assert [p["slug"] for p in found["items"]] == ["zeta"]
    by_bairro = (await client.get("/api/admin/points", params={"q": "centro"})).json()
    assert by_bairro["total"] == 3  # o bairro da fábrica é "Centro"
    active = (await client.get("/api/admin/points?status=active")).json()
    assert active["total"] == 2
    assert (await client.get("/api/admin/points?page_size=101")).status_code == 422
    # A listagem de pontos não grava acesso do admin.
    assert await events(app, type="admin_access") == []


# --- import do seed x pontos do admin ----------------------------------------------------


async def test_import_nao_mexe_nos_pontos_do_admin(
    app: FastAPI, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    await insert_point(app, "Editado no admin", slug="ponto-a", managed_by="admin")
    await insert_point(app, "Criado no admin", slug="ponto-c", managed_by="admin")
    await insert_point(app, "Do seed antigo", slug="ponto-velho")
    path = tmp_path / "seed.json"
    path.write_text(
        json.dumps([point_seed_data("ponto-a", name="Nome do seed"), point_seed_data("ponto-b")]),
        encoding="utf-8",
    )

    assert await run(path, apply=False) == 0
    output = capsys.readouterr().out
    assert "Mantidos pelo admin: 2" in output
    assert "mantido pelo admin (não muda): ponto-a" in output
    assert "mantido pelo admin (não muda): ponto-c" in output
    assert "A desativar: 1" in output  # só o ponto-velho

    code = await run(
        path,
        apply=True,
        allow_deactivate=True,
        confirm_host=safe_target(settings.MONGO_URL),
    )
    assert code == 0
    points = {p["slug"]: p async for p in app.state.db[POINTS].find({})}
    assert points["ponto-a"]["name"] == "Editado no admin"
    assert points["ponto-c"]["active"] is True
    assert points["ponto-b"]["active"] is True
    assert points["ponto-velho"]["active"] is False


# --- exportação CSV -------------------------------------------------------------------------


def parse_csv(response: httpx.Response) -> list[list[str]]:
    assert response.content.startswith(b"\xef\xbb\xbf")  # BOM para o Excel
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


async def test_csv_tem_cabecalho_bom_e_neutraliza_formulas(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    await insert_event(
        app,
        "login_failure",
        success=False,
        email="=HYPERLINK(1)",
        ip="@1.2.3.4",
        user_agent="+evil",
        reason="-x",
    )

    response = await client.get("/api/admin/logs/export", params={"type": "login_failure"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "utf-8" in response.headers["content-type"]
    disposition = response.headers["content-disposition"]
    assert re.fullmatch(r'attachment; filename="logs-oleoapp-\d{8}-\d{4}\.csv"', disposition)
    assert "x-export-truncated" not in response.headers
    rows = parse_csv(response)
    assert rows[0] == [
        "data_hora",
        "tipo",
        "sucesso",
        "usuario_id",
        "email",
        "ip",
        "navegador",
        "motivo",
        "metodo",
        "alvo_id",
    ]
    assert len(rows) == 2
    data = dict(zip(rows[0], rows[1], strict=True))
    assert data["tipo"] == "login_failure"
    assert data["sucesso"] == "nao"
    assert data["email"] == "'=HYPERLINK(1)"
    assert data["ip"] == "'@1.2.3.4"
    assert data["navegador"] == "'+evil"
    assert data["motivo"] == "'-x"
    assert data["data_hora"].endswith("-03:00")


async def test_csv_respeita_os_filtros_e_registra_o_acesso(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    old = datetime.now(UTC) - timedelta(days=10)
    await insert_event(app, "logout", created_at=old)
    await insert_event(app, "login_success")

    since = (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    rows = parse_csv(await client.get("/api/admin/logs/export", params={"from": since}))
    types = [row[1] for row in rows[1:]]
    assert "logout" not in types and "login_success" in types

    access = await events(app, type="admin_access", reason="/api/admin/logs/export")
    assert len(access) == 1
    assert (await client.get("/api/admin/logs/export?type=invalido")).status_code == 422


async def test_csv_corta_no_limite(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    await make_admin(app, client)
    for _ in range(3):
        await insert_event(app, "logout")
    monkeypatch.setattr(settings, "ADMIN_EXPORT_MAX_ROWS", 2)

    response = await client.get("/api/admin/logs/export", params={"type": "logout"})

    assert response.headers["x-export-truncated"] == "true"
    assert len(parse_csv(response)) == 3  # cabeçalho + 2 linhas


# --- alertas de falhas de login ------------------------------------------------------------


async def test_alertas_por_ip_e_por_email(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    await make_admin(app, client)
    monkeypatch.setattr(settings, "LOGIN_FAILURE_ALERT_THRESHOLD", 5)
    monkeypatch.setattr(settings, "LOGIN_FAILURE_ALERT_WINDOW_MIN", 15)
    now = datetime.now(UTC)

    for i in range(4):  # abaixo do limite
        await insert_event(app, "login_failure", success=False, ip="10.0.0.1", email=f"x{i}")
    for i in range(6):  # mesmo IP, e-mails diferentes
        await insert_event(app, "login_failure", success=False, ip="10.0.0.2", email=f"y{i}")
    for i in range(5):  # mesmo e-mail (mascarado), IPs diferentes
        await insert_event(
            app, "login_failure", success=False, ip=f"10.1.0.{i}", email="a***@x.com"
        )
    for _ in range(9):  # fora da janela: não conta
        await insert_event(
            app,
            "login_failure",
            success=False,
            ip="10.0.0.9",
            created_at=now - timedelta(minutes=30),
        )
    for _ in range(9):  # sucesso não é falha
        await insert_event(app, "login_success", ip="10.0.0.8")

    body = (await client.get("/api/admin/alerts")).json()

    assert body["window_min"] == 15 and body["threshold"] == 5
    assert [(i["kind"], i["key"], i["failures"]) for i in body["items"]] == [
        ("ip", "10.0.0.2", 6),
        ("email", "a***@x.com", 5),
    ]
    assert body["items"][0]["first_at"] <= body["items"][0]["last_at"]
    assert await events(app, type="admin_access") == []


async def test_sem_falhas_nao_ha_alerta(app: FastAPI, client: httpx.AsyncClient):
    await make_admin(app, client)
    assert (await client.get("/api/admin/alerts")).json()["items"] == []

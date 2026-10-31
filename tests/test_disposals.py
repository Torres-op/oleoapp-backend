from collections.abc import Callable
from datetime import timedelta

import httpx
from bson import ObjectId
from fastapi import FastAPI

from app.api.deps import CurrentUser
from app.core.impact_factors import IMPACT_FACTORS, IMPACT_SOURCES
from app.db.collections import DISPOSALS
from tests.factories import insert_disposal, insert_point

LoginAs = Callable[..., CurrentUser]


# --- criação -----------------------------------------------------------------


async def test_create_without_point(client: httpx.AsyncClient, login_as: LoginAs) -> None:
    login_as()
    response = await client.post("/api/disposals", json={"volume_ml": 4000})
    assert response.status_code == 201
    body = response.json()
    assert body["volume_ml"] == 4000
    assert body["point"] is None
    assert body["id"] and body["created_at"]


async def test_create_with_point_copies_name(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    login_as()
    point_id = await insert_point(app, name="Ponto Exemplo")
    response = await client.post("/api/disposals", json={"volume_ml": 2500, "point_id": point_id})
    assert response.status_code == 201
    assert response.json()["point"] == {"id": point_id, "name": "Ponto Exemplo"}

    saved = await app.state.db[DISPOSALS].find_one({})
    assert saved["point_name"] == "Ponto Exemplo"
    assert saved["point_id"] == ObjectId(point_id)


async def test_create_volume_limits(client: httpx.AsyncClient, login_as: LoginAs) -> None:
    login_as()
    for volume in (0, 10_001):
        response = await client.post("/api/disposals", json={"volume_ml": volume})
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "validation_error"
        assert error["fields"][0]["field"] == "volume_ml"
        assert error["fields"][0]["message"] == "Informe de 1 mL a 10 L por registro."
    for volume in (1, 10_000):
        response = await client.post("/api/disposals", json={"volume_ml": volume})
        assert response.status_code == 201


async def test_create_rejects_unknown_fields(client: httpx.AsyncClient, login_as: LoginAs) -> None:
    login_as()
    response = await client.post("/api/disposals", json={"volume_ml": 100, "user_id": "x"})
    assert response.status_code == 422


async def test_create_unknown_point_returns_404(
    client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    login_as()
    for point_id in (str(ObjectId()), "nao-e-um-id"):
        payload = {"volume_ml": 100, "point_id": point_id}
        response = await client.post("/api/disposals", json=payload)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


async def test_create_inactive_point_returns_404(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    login_as()
    point_id = await insert_point(app, active=False)
    response = await client.post("/api/disposals", json={"volume_ml": 100, "point_id": point_id})
    assert response.status_code == 404


async def test_requires_login(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/disposals")).status_code == 401
    assert (await client.post("/api/disposals", json={"volume_ml": 100})).status_code == 401
    assert (await client.get("/api/disposals/impact")).status_code == 401
    assert (await client.get("/api/disposals/reminder")).status_code == 401


# --- lista e exclusão --------------------------------------------------------


async def test_list_only_own_and_ordered(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    user = login_as()
    other_id = str(ObjectId())
    await insert_disposal(app, user.id, volume_ml=100, age=timedelta(days=3))
    await insert_disposal(app, user.id, volume_ml=300, age=timedelta(days=1))
    await insert_disposal(app, user.id, volume_ml=200, age=timedelta(days=2))
    await insert_disposal(app, other_id, volume_ml=999, age=timedelta(hours=1))

    response = await client.get("/api/disposals")
    assert response.status_code == 200
    assert [item["volume_ml"] for item in response.json()["items"]] == [300, 200, 100]


async def test_list_limit(app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs) -> None:
    user = login_as()
    for days in range(5):
        await insert_disposal(app, user.id, volume_ml=100 + days, age=timedelta(days=days))

    response = await client.get("/api/disposals", params={"limit": 2})
    assert [item["volume_ml"] for item in response.json()["items"]] == [100, 101]
    assert (await client.get("/api/disposals", params={"limit": 0})).status_code == 422
    assert (await client.get("/api/disposals", params={"limit": 201})).status_code == 422


async def test_delete_own_disposal(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    user = login_as()
    disposal_id = await insert_disposal(app, user.id)
    response = await client.delete(f"/api/disposals/{disposal_id}")
    assert response.status_code == 204
    assert await app.state.db[DISPOSALS].count_documents({}) == 0


async def test_delete_other_users_disposal_returns_404(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    login_as()
    disposal_id = await insert_disposal(app, str(ObjectId()))
    response = await client.delete(f"/api/disposals/{disposal_id}")
    assert response.status_code == 404
    assert await app.state.db[DISPOSALS].count_documents({}) == 1


async def test_delete_missing_or_invalid_id_returns_404(
    client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    login_as()
    for disposal_id in (str(ObjectId()), "id-invalido"):
        response = await client.delete(f"/api/disposals/{disposal_id}")
        assert response.status_code == 404


# --- impacto -----------------------------------------------------------------


async def test_impact_5_liters(app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs) -> None:
    user = login_as()
    await insert_disposal(app, user.id, volume_ml=2000)
    await insert_disposal(app, user.id, volume_ml=3000)
    await insert_disposal(app, str(ObjectId()), volume_ml=9000)  # de outro usuário

    response = await client.get("/api/disposals/impact", params={"period": "all"})
    assert response.status_code == 200
    body = response.json()
    assert body["period"] == "all"
    assert body["total_ml"] == 5000
    assert body["count"] == 2
    assert body["water_l"] == 5_000_000
    assert body["co2e_kg"] == 5.0
    assert body["factors_version"] == IMPACT_FACTORS["version"]
    assert body["factors"] == {"water_l_per_l_oluc": 1_000_000, "co2e_kg_per_l_oluc": 1.0}
    assert body["sources"] == IMPACT_SOURCES


async def test_impact_rounding(app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs) -> None:
    user = login_as()
    await insert_disposal(app, user.id, volume_ml=1234)
    body = (await client.get("/api/disposals/impact", params={"period": "all"})).json()
    assert body["water_l"] == 1_234_000
    assert body["co2e_kg"] == 1.2


async def test_impact_without_disposals(client: httpx.AsyncClient, login_as: LoginAs) -> None:
    login_as()
    body = (await client.get("/api/disposals/impact", params={"period": "12m"})).json()
    assert (body["total_ml"], body["count"], body["water_l"], body["co2e_kg"]) == (0, 0, 0, 0.0)


async def test_impact_period_filter(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    user = login_as()
    for days in (10, 100, 200, 400):  # cada um com 1 L
        await insert_disposal(app, user.id, volume_ml=1000, age=timedelta(days=days, hours=1))

    expected = {"30d": 1000, "6m": 2000, "12m": 3000, "all": 4000}
    for period, total_ml in expected.items():
        response = await client.get("/api/disposals/impact", params={"period": period})
        assert response.status_code == 200
        assert response.json()["total_ml"] == total_ml, period


async def test_impact_invalid_period_returns_422(
    client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    login_as()
    response = await client.get("/api/disposals/impact", params={"period": "5y"})
    assert response.status_code == 422


async def test_impact_numbers_are_not_stored(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    login_as()
    await client.post("/api/disposals", json={"volume_ml": 5000})
    saved = await app.state.db[DISPOSALS].find_one({})
    assert set(saved) == {"_id", "user_id", "point_id", "point_name", "volume_ml", "created_at"}


# --- lembrete ----------------------------------------------------------------


async def test_reminder_none(client: httpx.AsyncClient, login_as: LoginAs) -> None:
    login_as(reminder_interval_days=180)
    response = await client.get("/api/disposals/reminder")
    assert response.status_code == 200
    assert response.json() == {
        "status": "none",
        "last_disposal_at": None,
        "days_since": None,
        "days_left": None,
        "reminder_interval_days": 180,
    }


async def test_reminder_statuses_and_limits(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    # soon a partir de 144 dias (80%), overdue a partir de 180 (100%)
    user = login_as(reminder_interval_days=180)
    cases = [
        (timedelta(hours=2), "ok", 0),
        (timedelta(days=143, hours=12), "ok", 143),
        (timedelta(days=144, hours=1), "soon", 144),
        (timedelta(days=179, hours=12), "soon", 179),
        (timedelta(days=180, hours=1), "overdue", 180),
        (timedelta(days=400), "overdue", 400),
    ]
    for age, status, days_since in cases:
        await app.state.db[DISPOSALS].delete_many({})
        await insert_disposal(app, user.id, age=age)
        body = (await client.get("/api/disposals/reminder")).json()
        assert body["status"] == status, age
        assert body["days_since"] == days_since, age
        assert body["days_left"] == 180 - days_since, age
        assert body["reminder_interval_days"] == 180
        assert body["last_disposal_at"]


async def test_reminder_uses_latest_disposal_and_user_interval(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    user = login_as(reminder_interval_days=90)
    await insert_disposal(app, user.id, age=timedelta(days=200))
    await insert_disposal(app, user.id, age=timedelta(days=80, hours=1))  # o último
    await insert_disposal(app, str(ObjectId()), age=timedelta(hours=1))  # de outro usuário

    body = (await client.get("/api/disposals/reminder")).json()
    assert body["status"] == "soon"  # 80 >= 0,8 x 90 = 72
    assert body["days_since"] == 80
    assert body["days_left"] == 10
    assert body["reminder_interval_days"] == 90

from collections.abc import Callable
from datetime import timedelta

import httpx
from bson import ObjectId
from fastapi import FastAPI

from app.api.deps import CurrentUser
from app.db.collections import FAVORITES, POINTS
from tests.factories import CENTER_LAT, CENTER_LNG, insert_favorite, insert_point

LoginAs = Callable[..., CurrentUser]


async def test_requires_login(client: httpx.AsyncClient) -> None:
    point_id = str(ObjectId())
    assert (await client.get("/api/favorites")).status_code == 401
    assert (await client.put(f"/api/favorites/{point_id}")).status_code == 401
    assert (await client.delete(f"/api/favorites/{point_id}")).status_code == 401


async def test_add_and_list(app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs) -> None:
    login_as()
    point_id = await insert_point(app)

    response = await client.put(f"/api/favorites/{point_id}")
    assert response.status_code == 204
    assert response.content == b""

    items = (await client.get("/api/favorites")).json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["id"] == point_id
    assert item["name"] == "Ponto Exemplo"
    assert item["bairro"] == "Centro"
    assert item["address_formatted"] == "Rua Exemplo, 100 - Centro, Maricá - RJ, 24900-000"
    assert item["location"] == {"lat": CENTER_LAT, "lng": CENTER_LNG}
    assert item["accepted_oils"] == ["motor_carro", "motor_moto"]
    assert item["verified"] is True
    assert item["favorited_at"]
    assert "distance_m" not in item


async def test_add_twice_does_not_duplicate(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    login_as()
    point_id = await insert_point(app)

    assert (await client.put(f"/api/favorites/{point_id}")).status_code == 204
    first = await app.state.db[FAVORITES].find_one({})
    assert (await client.put(f"/api/favorites/{point_id}")).status_code == 204

    assert await app.state.db[FAVORITES].count_documents({}) == 1
    second = await app.state.db[FAVORITES].find_one({})
    assert second["created_at"] == first["created_at"]  # a data original é mantida
    assert len((await client.get("/api/favorites")).json()["items"]) == 1


async def test_add_unknown_point_returns_404(client: httpx.AsyncClient, login_as: LoginAs) -> None:
    login_as()
    for point_id in (str(ObjectId()), "nao-e-um-id"):
        response = await client.put(f"/api/favorites/{point_id}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


async def test_add_inactive_point_returns_404(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    login_as()
    point_id = await insert_point(app, active=False)
    response = await client.put(f"/api/favorites/{point_id}")
    assert response.status_code == 404
    assert await app.state.db[FAVORITES].count_documents({}) == 0


async def test_remove(app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs) -> None:
    login_as()
    point_id = await insert_point(app)
    await client.put(f"/api/favorites/{point_id}")

    assert (await client.delete(f"/api/favorites/{point_id}")).status_code == 204
    assert (await client.get("/api/favorites")).json() == {"items": []}


async def test_remove_missing_or_invalid_id_returns_204(
    client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    login_as()
    for point_id in (str(ObjectId()), "nao-e-um-id"):
        assert (await client.delete(f"/api/favorites/{point_id}")).status_code == 204


async def test_remove_only_affects_own_favorite(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    point_id = await insert_point(app)
    other_id = str(ObjectId())
    await insert_favorite(app, other_id, point_id)

    login_as()
    assert (await client.delete(f"/api/favorites/{point_id}")).status_code == 204
    assert await app.state.db[FAVORITES].count_documents({"user_id": ObjectId(other_id)}) == 1


async def test_list_hides_inactive_points(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    user = login_as()
    active_id = await insert_point(app, "Ativo")
    inactive_id = await insert_point(app, "Inativo")
    await insert_favorite(app, user.id, active_id, age=timedelta(days=1))
    await insert_favorite(app, user.id, inactive_id)
    await app.state.db[POINTS].update_one(
        {"_id": ObjectId(inactive_id)}, {"$set": {"active": False}}
    )

    items = (await client.get("/api/favorites")).json()["items"]
    assert [item["name"] for item in items] == ["Ativo"]


async def test_list_hides_deleted_points(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    user = login_as()
    await insert_favorite(app, user.id, str(ObjectId()))  # ponto que não existe mais
    assert (await client.get("/api/favorites")).json() == {"items": []}


async def test_list_only_own_favorites(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    user = login_as()
    mine = await insert_point(app, "Meu")
    theirs = await insert_point(app, "Dele")
    await insert_favorite(app, user.id, mine)
    await insert_favorite(app, str(ObjectId()), theirs)

    items = (await client.get("/api/favorites")).json()["items"]
    assert [item["name"] for item in items] == ["Meu"]


async def test_list_ordered_by_favorited_at_desc(
    app: FastAPI, client: httpx.AsyncClient, login_as: LoginAs
) -> None:
    user = login_as()
    oldest = await insert_point(app, "Antigo")
    middle = await insert_point(app, "Meio")
    newest = await insert_point(app, "Novo")
    await insert_favorite(app, user.id, middle, age=timedelta(days=2))
    await insert_favorite(app, user.id, newest, age=timedelta(days=1))
    await insert_favorite(app, user.id, oldest, age=timedelta(days=3))

    items = (await client.get("/api/favorites")).json()["items"]
    assert [item["name"] for item in items] == ["Novo", "Meio", "Antigo"]
    dates = [item["favorited_at"] for item in items]
    assert dates == sorted(dates, reverse=True)

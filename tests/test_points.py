from collections.abc import Callable

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.core.geo import haversine_km
from app.db.collections import POINTS
from app.repositories import points as points_repo
from app.schemas.points import PointSeed, format_address
from tests.factories import insert_point, point_seed_data

ORIGIN = (-22.9189, -42.8186)  # centro de Maricá


def make_seed(slug: str, lat: float, lng: float, **overrides) -> PointSeed:
    data = point_seed_data(
        slug,
        location={"lat": lat, "lng": lng},
        phone="(21) 0000-0000",
        opening_hours="Seg a Sex, 8h às 18h",
        accepted_oils=["motor_carro", "motor_moto"],
        conditions="Levar em recipiente fechado",
        **overrides,
    )
    return PointSeed.model_validate(data)


async def insert_points(app: FastAPI) -> dict[str, str]:
    """Três pontos a ~1,1 km, ~2,2 km e ~5,6 km da origem, mais um inativo a ~0,5 km."""
    db = app.state.db
    seeds = [
        make_seed("ponto-longe", ORIGIN[0] + 0.05, ORIGIN[1]),
        make_seed("ponto-perto", ORIGIN[0] + 0.01, ORIGIN[1]),
        make_seed("ponto-medio", ORIGIN[0] - 0.02, ORIGIN[1]),
        make_seed("ponto-inativo", ORIGIN[0] + 0.005, ORIGIN[1], active=False),
    ]
    for seed in seeds:
        await points_repo.upsert_by_slug(db, seed)
    ids = {}
    async for document in db[POINTS].find({}):
        ids[document["slug"]] = str(document["_id"])
    return ids


def expected_distance_m(lat: float, lng: float) -> float:
    return haversine_km(ORIGIN[0], ORIGIN[1], lat, lng) * 1000


# --- GET /api/points/near ---------------------------------------------------


async def test_near_ordena_por_distancia_e_segue_o_contrato(
    app: FastAPI, client: httpx.AsyncClient, login_as: Callable
):
    login_as()
    await insert_points(app)

    response = await client.get(
        "/api/points/near", params={"lat": ORIGIN[0], "lng": ORIGIN[1], "radius_m": 10_000}
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["name"] for item in items] == [
        "EXEMPLO - ponto-perto",
        "EXEMPLO - ponto-medio",
        "EXEMPLO - ponto-longe",
    ]
    first = items[0]
    assert set(first) == {
        "id",
        "name",
        "bairro",
        "address_formatted",
        "location",
        "accepted_oils",
        "verified",
        "distance_m",
    }
    assert isinstance(first["distance_m"], int)
    expected = expected_distance_m(ORIGIN[0] + 0.01, ORIGIN[1])
    assert first["distance_m"] == pytest.approx(expected, rel=0.01)
    assert first["bairro"] == "Centro"
    assert first["address_formatted"] == "Rua Exemplo, 100 - Centro, Maricá - RJ, 24900-000"
    assert first["location"]["lat"] == pytest.approx(ORIGIN[0] + 0.01)
    assert first["location"]["lng"] == pytest.approx(ORIGIN[1])
    assert first["accepted_oils"] == ["motor_carro", "motor_moto"]
    assert first["verified"] is True
    distances = [item["distance_m"] for item in items]
    assert distances == sorted(distances)


async def test_near_respeita_o_raio(app: FastAPI, client: httpx.AsyncClient, login_as: Callable):
    login_as()
    await insert_points(app)

    response = await client.get(
        "/api/points/near", params={"lat": ORIGIN[0], "lng": ORIGIN[1], "radius_m": 3_000}
    )

    names = [item["name"] for item in response.json()["items"]]
    assert names == ["EXEMPLO - ponto-perto", "EXEMPLO - ponto-medio"]


async def test_near_respeita_o_limite(app: FastAPI, client: httpx.AsyncClient, login_as: Callable):
    login_as()
    await insert_points(app)

    response = await client.get(
        "/api/points/near", params={"lat": ORIGIN[0], "lng": ORIGIN[1], "limit": 1}
    )

    items = response.json()["items"]
    assert [item["name"] for item in items] == ["EXEMPLO - ponto-perto"]


async def test_near_nao_devolve_pontos_inativos(
    app: FastAPI, client: httpx.AsyncClient, login_as: Callable
):
    login_as()
    await insert_points(app)

    response = await client.get("/api/points/near", params={"lat": ORIGIN[0], "lng": ORIGIN[1]})

    names = [item["name"] for item in response.json()["items"]]
    assert "EXEMPLO - ponto-inativo" not in names
    assert len(names) == 3


async def test_near_sem_pontos_devolve_lista_vazia(client: httpx.AsyncClient, login_as: Callable):
    login_as()
    response = await client.get("/api/points/near", params={"lat": ORIGIN[0], "lng": ORIGIN[1]})
    assert response.status_code == 200
    assert response.json() == {"items": []}


@pytest.mark.parametrize(
    "params",
    [
        {"lat": 91, "lng": ORIGIN[1]},
        {"lat": -91, "lng": ORIGIN[1]},
        {"lat": ORIGIN[0], "lng": 181},
        {"lat": ORIGIN[0]},
        {"lat": "abc", "lng": ORIGIN[1]},
        {"lat": ORIGIN[0], "lng": ORIGIN[1], "radius_m": 99},
        {"lat": ORIGIN[0], "lng": ORIGIN[1], "radius_m": 50_001},
        {"lat": ORIGIN[0], "lng": ORIGIN[1], "limit": 0},
        {"lat": ORIGIN[0], "lng": ORIGIN[1], "limit": 101},
    ],
)
async def test_near_com_parametros_invalidos_responde_422(
    client: httpx.AsyncClient, login_as: Callable, params: dict
):
    login_as()
    response = await client.get("/api/points/near", params=params)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_near_lat_fora_do_intervalo_lista_o_campo(
    client: httpx.AsyncClient, login_as: Callable
):
    login_as()
    response = await client.get("/api/points/near", params={"lat": 91, "lng": ORIGIN[1]})
    fields = response.json()["error"]["fields"]
    assert len(fields) == 1
    assert fields[0]["field"] == "lat"
    assert fields[0]["message"].startswith("O valor máximo é 90")


async def test_near_exige_login(client: httpx.AsyncClient):
    response = await client.get("/api/points/near", params={"lat": ORIGIN[0], "lng": ORIGIN[1]})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_near_tolera_documento_fora_do_padrao_no_banco(
    app: FastAPI, client: httpx.AsyncClient, login_as: Callable
):
    # Documento gravado sem passar pelo seed: sem bairro, cidade e UF, CEP fora do
    # formato, número salvo como inteiro e um campo a mais. Não pode virar 500.
    login_as()
    point_id = await insert_point(
        app,
        "EXEMPLO - incompleto",
        lat=ORIGIN[0] + 0.01,
        lng=ORIGIN[1],
        address={"street": "Rua Sem Bairro", "number": 7, "cep": "123", "complemento": "fundos"},
    )
    await insert_point(
        app, "EXEMPLO - sem endereço", lat=ORIGIN[0] + 0.02, lng=ORIGIN[1], address=None
    )

    response = await client.get("/api/points/near", params={"lat": ORIGIN[0], "lng": ORIGIN[1]})

    assert response.status_code == 200
    items = response.json()["items"]
    assert [item["name"] for item in items] == ["EXEMPLO - incompleto", "EXEMPLO - sem endereço"]
    assert items[0]["bairro"] == ""
    assert items[0]["address_formatted"] == "Rua Sem Bairro, 7, 123"
    assert items[1]["address_formatted"] == ""

    detail = await client.get(f"/api/points/{point_id}")
    assert detail.status_code == 200
    assert detail.json()["address"] == {
        "street": "Rua Sem Bairro",
        "number": "7",
        "bairro": "",
        "city": "",
        "state": "",
        "cep": "123",
        "formatted": "Rua Sem Bairro, 7, 123",
    }


# --- GET /api/points/{point_id} ---------------------------------------------


async def test_get_point_devolve_o_ponto_completo(
    app: FastAPI, client: httpx.AsyncClient, login_as: Callable
):
    login_as()
    ids = await insert_points(app)

    response = await client.get(f"/api/points/{ids['ponto-perto']}")

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "id",
        "slug",
        "name",
        "address",
        "location",
        "phone",
        "opening_hours",
        "accepted_oils",
        "conditions",
        "verified",
        "verified_at",
        "google_place_id",
        "updated_at",
    }
    assert body["id"] == ids["ponto-perto"]
    assert body["slug"] == "ponto-perto"
    assert body["address"] == {
        "street": "Rua Exemplo",
        "number": "100",
        "bairro": "Centro",
        "city": "Maricá",
        "state": "RJ",
        "cep": "24900-000",
        "formatted": "Rua Exemplo, 100 - Centro, Maricá - RJ, 24900-000",
    }
    assert body["location"]["lat"] == pytest.approx(ORIGIN[0] + 0.01)
    assert body["location"]["lng"] == pytest.approx(ORIGIN[1])
    assert body["verified_at"] == "2026-10-01T00:00:00Z"
    assert body["updated_at"].endswith("Z")
    assert body["google_place_id"] is None
    assert "created_at" not in body


@pytest.mark.parametrize("point_id", ["abc", "123", "zzzzzzzzzzzzzzzzzzzzzzzz"])
async def test_get_point_com_id_malformado_responde_404(
    client: httpx.AsyncClient, login_as: Callable, point_id: str
):
    login_as()
    response = await client.get(f"/api/points/{point_id}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_get_point_inexistente_responde_404(client: httpx.AsyncClient, login_as: Callable):
    login_as()
    response = await client.get(f"/api/points/{ObjectId()}")
    assert response.status_code == 404
    assert response.json()["error"] == {
        "code": "not_found",
        "message": "Ponto de coleta não encontrado.",
        "fields": None,
    }


async def test_get_point_inativo_responde_404(
    app: FastAPI, client: httpx.AsyncClient, login_as: Callable
):
    login_as()
    ids = await insert_points(app)
    response = await client.get(f"/api/points/{ids['ponto-inativo']}")
    assert response.status_code == 404


async def test_get_point_exige_login(client: httpx.AsyncClient):
    response = await client.get(f"/api/points/{ObjectId()}")
    assert response.status_code == 401


# --- schema -----------------------------------------------------------------


def test_format_address_omite_partes_vazias():
    assert (
        format_address("Rua Exemplo", "100", "Centro", "Maricá", "RJ", "24900-000")
        == "Rua Exemplo, 100 - Centro, Maricá - RJ, 24900-000"
    )
    assert (
        format_address("Rua Exemplo", None, "Centro", "Maricá", "RJ", None)
        == "Rua Exemplo - Centro, Maricá - RJ"
    )
    assert format_address("Rua Exemplo", "", "", "Maricá", "RJ", "") == "Rua Exemplo, Maricá - RJ"
    assert format_address("", None, None, "", "", None) == ""


async def test_indice_2dsphere_guarda_longitude_primeiro(app: FastAPI):
    await insert_points(app)
    document = await app.state.db[POINTS].find_one({"slug": "ponto-perto"})
    assert document["location"] == {
        "type": "Point",
        "coordinates": [pytest.approx(ORIGIN[1]), pytest.approx(ORIGIN[0] + 0.01)],
    }
    assert document["created_at"] is not None
    assert document["accepted_oils"] == ["motor_carro", "motor_moto"]

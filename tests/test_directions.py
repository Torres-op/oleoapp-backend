import json
import logging
from collections.abc import Callable

import httpx
import pytest
from bson import ObjectId
from fastapi import FastAPI

from app.core.config import settings
from tests.factories import insert_point

ORIGIN = {"lat": -22.9189, "lng": -42.8186}  # centro de Maricá
POINT_LAT, POINT_LNG = -22.9050, -42.8200
ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
API_KEY = "chave-de-teste"

GOOGLE_OK = {
    "routes": [
        {
            "distanceMeters": 5400,
            "duration": "780s",
            "polyline": {"encodedPolyline": "abc123"},
        }
    ]
}


class GoogleMock:
    """Substitui o cliente HTTP da aplicação e guarda as requisições recebidas."""

    def __init__(self, status: int = 200, payload: dict | None = None) -> None:
        self.status = status
        self.payload = GOOGLE_OK if payload is None else payload
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json=self.payload)


@pytest.fixture
def google(http_mock: Callable) -> Callable[..., GoogleMock]:
    def install(mock: GoogleMock | None = None, handler=None) -> GoogleMock:
        mock = mock or GoogleMock()
        http_mock(handler or mock.handler)
        return mock

    return install


@pytest.fixture(autouse=True)
def _api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", API_KEY)


def body(point_id: str, origin: dict | None = None) -> dict:
    return {"origin": origin or ORIGIN, "point_id": point_id}


async def test_rota_com_sucesso(
    app: FastAPI, client: httpx.AsyncClient, login_as: Callable, google: Callable
):
    login_as()
    point_id = await insert_point(app, lat=POINT_LAT, lng=POINT_LNG)
    mock = google()

    response = await client.post("/api/directions", json=body(point_id))

    assert response.status_code == 200
    assert response.json() == {"encoded_polyline": "abc123", "distance_m": 5400, "duration_s": 780}

    assert len(mock.requests) == 1
    request = mock.requests[0]
    assert request.method == "POST"
    assert str(request.url) == ROUTES_URL
    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-goog-api-key"] == API_KEY
    assert (
        request.headers["x-goog-fieldmask"]
        == "routes.distanceMeters,routes.duration,routes.polyline.encodedPolyline"
    )
    sent = json.loads(request.content)
    assert sent == {
        "origin": {"location": {"latLng": {"latitude": ORIGIN["lat"], "longitude": ORIGIN["lng"]}}},
        # O destino vem do banco (GeoJSON [lng, lat]), não do cliente.
        "destination": {"location": {"latLng": {"latitude": POINT_LAT, "longitude": POINT_LNG}}},
        "travelMode": "DRIVE",
        "languageCode": "pt-BR",
        "units": "METRIC",
    }
    assert API_KEY not in response.text


async def test_distancia_ausente_vira_null(
    app: FastAPI, client: httpx.AsyncClient, login_as: Callable, google: Callable
):
    login_as()
    point_id = await insert_point(app, lat=POINT_LAT, lng=POINT_LNG)
    google(GoogleMock(payload={"routes": [{"polyline": {"encodedPolyline": "xyz"}}]}))

    response = await client.post("/api/directions", json=body(point_id))

    assert response.status_code == 200
    assert response.json() == {"encoded_polyline": "xyz", "distance_m": None, "duration_s": None}


async def test_origem_fora_da_area_responde_400(
    app: FastAPI, client: httpx.AsyncClient, login_as: Callable, google: Callable
):
    login_as()
    point_id = await insert_point(app, lat=POINT_LAT, lng=POINT_LNG)
    mock = google()

    response = await client.post(
        "/api/directions", json=body(point_id, {"lat": -23.5505, "lng": -46.6333})
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "out_of_area"
    assert response.json()["error"]["message"] == "A rota no app só funciona em Maricá e arredores."
    assert mock.requests == []


async def test_ponto_inexistente_responde_404(
    client: httpx.AsyncClient, login_as: Callable, google: Callable
):
    login_as()
    mock = google()

    response = await client.post("/api/directions", json=body(str(ObjectId())))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert response.json()["error"]["message"] == "Ponto de coleta não encontrado."
    assert mock.requests == []


async def test_id_invalido_responde_404(
    client: httpx.AsyncClient, login_as: Callable, google: Callable
):
    login_as()
    mock = google()

    response = await client.post("/api/directions", json=body("nao-e-um-objectid"))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert mock.requests == []


async def test_ponto_inativo_responde_404(
    app: FastAPI, client: httpx.AsyncClient, login_as: Callable, google: Callable
):
    login_as()
    point_id = await insert_point(app, lat=POINT_LAT, lng=POINT_LNG, active=False)
    mock = google()

    response = await client.post("/api/directions", json=body(point_id))

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert mock.requests == []


async def test_google_500_responde_502_sem_vazar_detalhes(
    app: FastAPI,
    client: httpx.AsyncClient,
    login_as: Callable,
    google: Callable,
    caplog: pytest.LogCaptureFixture,
):
    login_as()
    point_id = await insert_point(app, lat=POINT_LAT, lng=POINT_LNG)
    google(GoogleMock(status=500, payload={"error": {"message": "mensagem interna do Google"}}))

    with caplog.at_level(logging.INFO):
        response = await client.post("/api/directions", json=body(point_id))

    assert response.status_code == 502
    assert response.json()["error"] == {
        "code": "upstream_error",
        "message": "Não foi possível calcular a rota agora.",
        "fields": None,
    }
    assert "mensagem interna do Google" not in response.text
    assert "500" in caplog.text
    # Nunca logar coordenadas, nem a mensagem do Google.
    secrets = (str(ORIGIN["lat"]), str(ORIGIN["lng"]), str(POINT_LAT), API_KEY, "mensagem interna")
    for secret in secrets:
        assert secret not in caplog.text


async def test_google_sem_polilinha_responde_502(
    app: FastAPI, client: httpx.AsyncClient, login_as: Callable, google: Callable
):
    login_as()
    point_id = await insert_point(app, lat=POINT_LAT, lng=POINT_LNG)
    google(GoogleMock(payload={"routes": []}))

    response = await client.post("/api/directions", json=body(point_id))

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


async def test_erro_de_rede_responde_502(
    app: FastAPI, client: httpx.AsyncClient, login_as: Callable, google: Callable
):
    login_as()
    point_id = await insert_point(app, lat=POINT_LAT, lng=POINT_LNG)

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("falha de conexão", request=request)

    google(handler=boom)

    response = await client.post("/api/directions", json=body(point_id))

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


async def test_sem_chave_responde_503(
    app: FastAPI,
    client: httpx.AsyncClient,
    login_as: Callable,
    google: Callable,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", "")
    login_as()
    point_id = await insert_point(app, lat=POINT_LAT, lng=POINT_LNG)
    mock = google()

    response = await client.post("/api/directions", json=body(point_id))

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert response.json()["error"]["message"] == "Rotas indisponíveis no momento."
    assert mock.requests == []


async def test_nao_aceita_coordenadas_de_destino_do_cliente(
    app: FastAPI, client: httpx.AsyncClient, login_as: Callable, google: Callable
):
    login_as()
    point_id = await insert_point(app, lat=POINT_LAT, lng=POINT_LNG)
    mock = google()

    payload = {**body(point_id), "destination": {"lat": -22.9, "lng": -42.8}}
    response = await client.post("/api/directions", json=payload)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"
    assert mock.requests == []


async def test_exige_login(client: httpx.AsyncClient, google: Callable):
    mock = google()

    response = await client.post("/api/directions", json=body(str(ObjectId())))

    assert response.status_code == 401
    assert mock.requests == []


async def test_429_apos_o_limite_por_usuario(
    app: FastAPI,
    client: httpx.AsyncClient,
    login_as: Callable,
    google: Callable,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "RATE_LIMIT_DIRECTIONS_PER_HOUR", 1)
    login_as()
    point_id = await insert_point(app, lat=POINT_LAT, lng=POINT_LNG)
    mock = google()

    first = await client.post("/api/directions", json=body(point_id))
    blocked = await client.post("/api/directions", json=body(point_id))

    assert first.status_code == 200
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked.headers["retry-after"]) >= 1
    assert len(mock.requests) == 1  # a requisição bloqueada não chegou ao Google


async def test_requisicao_invalida_nao_gasta_a_cota(
    app: FastAPI,
    client: httpx.AsyncClient,
    login_as: Callable,
    google: Callable,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "RATE_LIMIT_DIRECTIONS_PER_HOUR", 1)
    login_as()
    point_id = await insert_point(app, lat=POINT_LAT, lng=POINT_LNG)
    mock = google()

    for _ in range(3):
        invalid = await client.post("/api/directions", json={"point_id": point_id})
        assert invalid.status_code == 422

    response = await client.post("/api/directions", json=body(point_id))

    assert response.status_code == 200
    assert len(mock.requests) == 1

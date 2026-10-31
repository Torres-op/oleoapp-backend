import hashlib
import logging
from collections.abc import Callable

import httpx
import pytest
from fastapi import FastAPI

from app.api.deps import CurrentUser, get_current_user
from app.core.config import settings
from app.core.logging import configure_logging

LoginAs = Callable[..., CurrentUser]

API_KEY = "chave-de-teste"
CENTER = {"lat": -22.9189, "lng": -42.8186}  # centro de Maricá
SAO_PAULO = {"lat": -23.5505, "lng": -46.6333}  # bem fora da área atendida

VIACEP_MARICA = {
    "cep": "24900-000",
    "logradouro": "Rua Exemplo",
    "bairro": "Centro",
    "localidade": "Maricá",
    "uf": "RJ",
}


def google_payload(*places: tuple[str, dict]) -> dict:
    return {
        "status": "OK",
        "results": [
            {"formatted_address": label, "geometry": {"location": point}} for label, point in places
        ],
    }


def nominatim_payload(*places: tuple[str, dict]) -> list[dict]:
    return [
        {"display_name": label, "lat": str(point["lat"]), "lon": str(point["lng"])}
        for label, point in places
    ]


class ProviderMock:
    """Responde por host e guarda as requisições recebidas."""

    def __init__(self) -> None:
        self.responses: dict[str, tuple[int, object]] = {}
        self.requests: list[httpx.Request] = []

    def set(self, host: str, payload: object, status: int = 200) -> None:
        self.responses[host] = (status, payload)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, payload = self.responses[request.url.host]
        return httpx.Response(status, json=payload)

    def hosts(self) -> list[str]:
        return [request.url.host for request in self.requests]


@pytest.fixture
def providers(http_mock: Callable) -> ProviderMock:
    mock = ProviderMock()
    http_mock(mock.handler)
    return mock


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch, login_as: LoginAs) -> None:
    monkeypatch.setattr(settings, "GEOCODER", "google")
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", API_KEY)
    monkeypatch.setattr(settings, "NOMINATIM_USER_AGENT", "OleoApp-Teste/1.0")
    login_as()


async def test_requires_login(app: FastAPI, client: httpx.AsyncClient) -> None:
    app.dependency_overrides.pop(get_current_user, None)
    response = await client.get("/api/geocode", params={"q": "Centro"})
    assert response.status_code == 401


# --- CEP ---------------------------------------------------------------------


@pytest.mark.parametrize("cep", ["24900-000", "24900000"])
async def test_cep_in_marica(client: httpx.AsyncClient, providers: ProviderMock, cep: str) -> None:
    providers.set("viacep.com.br", VIACEP_MARICA)
    providers.set(
        "maps.googleapis.com", google_payload(("Rua Exemplo, Centro, Maricá - RJ", CENTER))
    )

    response = await client.get("/api/geocode", params={"q": cep})
    assert response.status_code == 200
    assert response.json() == {"items": [{"label": "Rua Exemplo, Centro, Maricá - RJ", **CENTER}]}

    assert providers.hosts() == ["viacep.com.br", "maps.googleapis.com"]
    assert providers.requests[0].url.path == "/ws/24900000/json/"
    assert providers.requests[1].url.params["address"] == "Rua Exemplo, Centro, Maricá - RJ"


async def test_cep_without_street_uses_city_only(
    client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    providers.set(
        "viacep.com.br", {"logradouro": "", "bairro": "", "localidade": "MARICA", "uf": "RJ"}
    )
    providers.set("maps.googleapis.com", google_payload(("Maricá - RJ", CENTER)))

    response = await client.get("/api/geocode", params={"q": "24900-000"})
    assert len(response.json()["items"]) == 1
    assert providers.requests[1].url.params["address"] == "Maricá - RJ"


async def test_cep_from_other_city_returns_empty(
    client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    providers.set(
        "viacep.com.br",
        {"logradouro": "Rua X", "bairro": "Icaraí", "localidade": "Niterói", "uf": "RJ"},
    )

    response = await client.get("/api/geocode", params={"q": "24220-000"})
    assert response.status_code == 200
    assert response.json() == {"items": []}
    assert providers.hosts() == ["viacep.com.br"]  # nem chegou a geocodificar


@pytest.mark.parametrize("erro", [True, "true"])
async def test_unknown_cep_returns_empty(
    client: httpx.AsyncClient, providers: ProviderMock, erro: object
) -> None:
    providers.set("viacep.com.br", {"erro": erro})

    response = await client.get("/api/geocode", params={"q": "99999-999"})
    assert response.json() == {"items": []}
    assert providers.hosts() == ["viacep.com.br"]


# --- texto livre -------------------------------------------------------------


async def test_free_text_google(client: httpx.AsyncClient, providers: ProviderMock) -> None:
    providers.set(
        "maps.googleapis.com",
        google_payload(("Centro, Maricá - RJ, Brasil", CENTER)),
    )

    response = await client.get("/api/geocode", params={"q": "  Centro   de  Maricá "})
    assert response.status_code == 200
    assert response.json() == {"items": [{"label": "Centro, Maricá - RJ, Brasil", **CENTER}]}

    assert providers.hosts() == ["maps.googleapis.com"]  # texto livre não consulta o ViaCEP
    params = providers.requests[0].url.params
    assert params["address"] == "Centro de Maricá"  # espaços normalizados
    assert params["key"] == API_KEY
    assert params["language"] == "pt-BR"
    assert params["region"] == "br"
    assert params["components"] == "country:BR|administrative_area:RJ"


async def test_google_zero_results(client: httpx.AsyncClient, providers: ProviderMock) -> None:
    providers.set("maps.googleapis.com", {"status": "ZERO_RESULTS", "results": []})
    response = await client.get("/api/geocode", params={"q": "lugar nenhum"})
    assert response.status_code == 200
    assert response.json() == {"items": []}


async def test_free_text_nominatim(
    client: httpx.AsyncClient, providers: ProviderMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "GEOCODER", "nominatim")
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", "")  # sem chave não pode atrapalhar
    providers.set(
        "nominatim.openstreetmap.org",
        nominatim_payload(("Centro, Maricá, Rio de Janeiro, Brasil", CENTER)),
    )

    response = await client.get("/api/geocode", params={"q": "Centro"})
    assert response.status_code == 200
    assert response.json() == {
        "items": [{"label": "Centro, Maricá, Rio de Janeiro, Brasil", **CENTER}]
    }

    request = providers.requests[0]
    assert providers.hosts() == ["nominatim.openstreetmap.org"]
    assert request.headers["User-Agent"] == "OleoApp-Teste/1.0"
    params = request.url.params
    assert params["q"] == "Centro"
    assert params["format"] == "jsonv2"
    assert params["countrycodes"] == "br"
    assert params["limit"] == "5"
    assert params["bounded"] == "1"
    lng_min, lat_max, lng_max, lat_min = (float(v) for v in params["viewbox"].split(","))
    assert lng_min < CENTER["lng"] < lng_max
    assert lat_min < CENTER["lat"] < lat_max


# --- filtro de área e limite -------------------------------------------------


async def test_results_outside_area_are_discarded(
    client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    providers.set(
        "maps.googleapis.com",
        google_payload(("Av. Paulista, São Paulo - SP", SAO_PAULO), ("Centro, Maricá", CENTER)),
    )
    items = (await client.get("/api/geocode", params={"q": "avenida"})).json()["items"]
    assert [item["label"] for item in items] == ["Centro, Maricá"]


async def test_nominatim_results_outside_area_are_discarded(
    client: httpx.AsyncClient, providers: ProviderMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "GEOCODER", "nominatim")
    providers.set(
        "nominatim.openstreetmap.org",
        nominatim_payload(("Av. Paulista", SAO_PAULO), ("Centro, Maricá", CENTER)),
    )
    items = (await client.get("/api/geocode", params={"q": "avenida"})).json()["items"]
    assert [item["label"] for item in items] == ["Centro, Maricá"]


async def test_all_results_outside_area_returns_empty(
    client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    providers.set("maps.googleapis.com", google_payload(("São Paulo - SP", SAO_PAULO)))
    response = await client.get("/api/geocode", params={"q": "paulista"})
    assert response.json() == {"items": []}


async def test_at_most_five_results(client: httpx.AsyncClient, providers: ProviderMock) -> None:
    places = [(f"Lugar {i}, Maricá", CENTER) for i in range(8)]
    providers.set("maps.googleapis.com", google_payload(*places))
    items = (await client.get("/api/geocode", params={"q": "lugar"})).json()["items"]
    assert [item["label"] for item in items] == [f"Lugar {i}, Maricá" for i in range(5)]


# --- cache -------------------------------------------------------------------


async def test_cache_skips_provider_on_second_call(
    app: FastAPI, client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    if app.state.redis is None:
        pytest.skip("Sem Redis não há cache.")
    providers.set("maps.googleapis.com", google_payload(("Centro, Maricá", CENTER)))

    first = await client.get("/api/geocode", params={"q": "Centro Maricá"})
    assert len(providers.requests) == 1
    # Mesma busca com outra caixa e espaços diferentes: a chave normalizada é a mesma.
    second = await client.get("/api/geocode", params={"q": "  centro   MARICÁ "})

    assert first.status_code == second.status_code == 200
    assert second.json() == first.json()
    assert len(providers.requests) == 1  # a segunda chamada não chegou ao provedor

    key = "geocode:v1:" + hashlib.sha1("centro maricá".encode()).hexdigest()
    assert await app.state.redis.ttl(key) > 6 * 24 * 3600  # TTL de 7 dias


async def test_empty_result_is_cached_too(
    app: FastAPI, client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    if app.state.redis is None:
        pytest.skip("Sem Redis não há cache.")
    providers.set("viacep.com.br", {"erro": "true"})

    await client.get("/api/geocode", params={"q": "99999-999"})
    response = await client.get("/api/geocode", params={"q": "99999-999"})
    assert response.json() == {"items": []}
    assert len(providers.requests) == 1


async def test_upstream_errors_are_not_cached(
    app: FastAPI, client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    if app.state.redis is None:
        pytest.skip("Sem Redis não há cache.")
    providers.set("maps.googleapis.com", {}, status=500)
    assert (await client.get("/api/geocode", params={"q": "Centro"})).status_code == 502

    providers.set("maps.googleapis.com", google_payload(("Centro, Maricá", CENTER)))
    response = await client.get("/api/geocode", params={"q": "Centro"})
    assert response.status_code == 200
    assert len(response.json()["items"]) == 1


# --- erros -------------------------------------------------------------------


async def test_google_without_key_returns_503(
    client: httpx.AsyncClient, providers: ProviderMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", "")
    response = await client.get("/api/geocode", params={"q": "Centro"})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    assert providers.requests == []


@pytest.mark.parametrize("q", ["ab", " a ", "x" * 121])
async def test_invalid_q_length_returns_422(client: httpx.AsyncClient, q: str) -> None:
    response = await client.get("/api/geocode", params={"q": q})
    assert response.status_code == 422


async def test_q_too_short_after_trimming_lists_the_field(
    client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    response = await client.get("/api/geocode", params={"q": "  ab  "})

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["fields"] == [{"field": "q", "message": "Use pelo menos 3 caracteres."}]
    assert providers.requests == []


async def test_missing_q_returns_422(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/geocode")).status_code == 422


@pytest.mark.parametrize(
    ("host", "payload", "status"),
    [
        ("maps.googleapis.com", {}, 500),
        ("maps.googleapis.com", {"status": "REQUEST_DENIED", "results": []}, 200),
        ("maps.googleapis.com", {"status": "OVER_QUERY_LIMIT"}, 200),
        ("maps.googleapis.com", ["não é objeto"], 200),
    ],
)
async def test_google_failures_return_502(
    client: httpx.AsyncClient, providers: ProviderMock, host: str, payload: object, status: int
) -> None:
    providers.set(host, payload, status=status)
    response = await client.get("/api/geocode", params={"q": "Centro"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


async def test_nominatim_failure_returns_502(
    client: httpx.AsyncClient, providers: ProviderMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "GEOCODER", "nominatim")
    providers.set("nominatim.openstreetmap.org", {}, status=503)
    response = await client.get("/api/geocode", params={"q": "Centro"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


async def test_viacep_failure_returns_502(
    client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    providers.set("viacep.com.br", {}, status=500)
    response = await client.get("/api/geocode", params={"q": "24900-000"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


async def test_network_error_returns_502_without_leaking_query(
    client: httpx.AsyncClient, http_mock: Callable, caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("falhou", request=request)

    http_mock(handler)

    with caplog.at_level(logging.DEBUG):
        response = await client.get("/api/geocode", params={"q": "Rua Secreta 123"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"
    assert "Rua Secreta" not in caplog.text
    assert API_KEY not in caplog.text


async def test_success_does_not_log_key_or_query(
    client: httpx.AsyncClient, providers: ProviderMock, caplog: pytest.LogCaptureFixture
) -> None:
    # O httpx registra a URL completa em INFO; com a chave e o texto na query string,
    # isso vazaria os dois. configure_logging sobe o httpx e o httpcore para WARNING.
    configure_logging()
    caplog.set_level(logging.INFO)
    providers.set("maps.googleapis.com", google_payload(("Rua Secreta, Maricá", CENTER)))

    response = await client.get("/api/geocode", params={"q": "Rua Secreta 123"})

    assert response.status_code == 200
    assert API_KEY not in caplog.text
    assert "Secreta" not in caplog.text
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


async def test_invalid_request_does_not_use_quota(
    client: httpx.AsyncClient, providers: ProviderMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_GEOCODE_PER_MIN", 1)
    providers.set("maps.googleapis.com", google_payload(("Centro, Maricá", CENTER)))

    for _ in range(3):
        assert (await client.get("/api/geocode", params={"q": "ab"})).status_code == 422

    assert (await client.get("/api/geocode", params={"q": "Centro"})).status_code == 200
    blocked = await client.get("/api/geocode", params={"q": "Centro 2"})
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"


# --- sugestões enquanto digita (Photon) -----------------------------------------

PHOTON_HOST = "photon.komoot.io"
SAO_GONCALO = {"lat": -22.8268, "lng": -43.0634}  # vizinha, dentro do raio de 40 km


def photon_feature(point: dict, **props: str) -> dict:
    return {
        "type": "Feature",
        "properties": {"country": "Brasil", "state": "Rio de Janeiro", **props},
        "geometry": {"type": "Point", "coordinates": [point["lng"], point["lat"]]},
    }


def photon_payload(*features: dict) -> dict:
    return {"type": "FeatureCollection", "features": list(features)}


async def test_suggest_requires_login(app: FastAPI, client: httpx.AsyncClient) -> None:
    app.dependency_overrides.pop(get_current_user, None)
    response = await client.get("/api/geocode/suggest", params={"q": "Rua Abreu"})
    assert response.status_code == 401


async def test_suggest_splits_brazilian_address(
    client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    providers.set(
        PHOTON_HOST,
        photon_payload(
            photon_feature(
                CENTER,
                type="street",
                name="Rua Abreu Rangel",
                district="Centro",
                city="Maricá",
                postcode="24900890",
            ),
            photon_feature(
                CENTER,
                type="house",
                name="Posto Exemplo",
                street="Rua Abreu Rangel",
                housenumber="120",
                district="Centro",
                city="Maricá",
                postcode="24900-890",
            ),
            photon_feature(CENTER, type="district", name="Itaipuaçu", city="Maricá"),
        ),
    )

    response = await client.get("/api/geocode/suggest", params={"q": "Rua Abreu"})

    assert response.status_code == 200
    street, house, area = response.json()["items"]
    assert street == {
        "label": "Rua Abreu Rangel - Centro, Maricá - RJ, 24900-890",
        "lat": CENTER["lat"],
        "lng": CENTER["lng"],
        "kind": "street",
        "street": "Rua Abreu Rangel",
        "number": None,
        "bairro": "Centro",
        "city": "Maricá",
        "state": "RJ",
        "cep": "24900-890",
    }
    assert house["kind"] == "address"
    assert house["number"] == "120"
    assert house["label"] == (
        "Posto Exemplo - Rua Abreu Rangel, 120 - Centro, Maricá - RJ, 24900-890"
    )
    assert area["kind"] == "area"
    assert area["bairro"] == "Itaipuaçu"
    assert area["street"] is None
    assert area["label"] == "Itaipuaçu, Maricá - RJ"


async def test_suggest_sends_area_filters_to_photon(
    client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    providers.set(PHOTON_HOST, photon_payload())

    await client.get("/api/geocode/suggest", params={"q": "  rua   abreu "})

    (request,) = providers.requests
    params = request.url.params
    assert params["q"] == "rua abreu"
    assert params["bbox"].count(",") == 3
    assert params.get_list("layer") == ["house", "street", "district", "locality", "city"]
    assert request.headers["User-Agent"] == "OleoApp-Teste/1.0"


async def test_suggest_filters_area_dedupes_and_puts_marica_first(
    client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    providers.set(
        PHOTON_HOST,
        photon_payload(
            photon_feature(SAO_GONCALO, type="street", name="Rua Flamengo", city="São Gonçalo"),
            photon_feature(SAO_PAULO, type="street", name="Rua Flamengo", city="São Paulo"),
            photon_feature(CENTER, type="street", name="Rua Flamengo", city="Maricá"),
            photon_feature(CENTER, type="street", name="Rua Flamengo", city="Maricá"),
            photon_feature(CENTER, type="house", name="Sem rua"),  # sem rua: descartado
            photon_feature(CENTER, type="country", name="Brasil"),  # tipo ignorado
        ),
    )

    items = (await client.get("/api/geocode/suggest", params={"q": "flamengo"})).json()["items"]

    assert [item["city"] for item in items] == ["Maricá", "São Gonçalo"]


async def test_suggest_cep_goes_to_regular_search(
    client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    response = await client.get("/api/geocode/suggest", params={"q": "24900-000"})
    assert response.json() == {"items": []}
    assert providers.requests == []


async def test_suggest_is_cached(
    app: FastAPI, client: httpx.AsyncClient, providers: ProviderMock
) -> None:
    if app.state.redis is None:
        pytest.skip("Sem Redis não há cache.")
    providers.set(
        PHOTON_HOST,
        photon_payload(photon_feature(CENTER, type="street", name="Rua A", city="Maricá")),
    )

    first = await client.get("/api/geocode/suggest", params={"q": "Rua A Marica"})
    second = await client.get("/api/geocode/suggest", params={"q": " rua a  MARICA "})

    assert second.json() == first.json()
    assert len(providers.requests) == 1
    key = "geosuggest:v1:" + hashlib.sha1(b"rua a marica").hexdigest()
    assert await app.state.redis.ttl(key) > 23 * 3600


@pytest.mark.parametrize(("payload", "status"), [({}, 500), ({"sem": "features"}, 200)])
async def test_suggest_photon_failure_returns_502(
    client: httpx.AsyncClient, providers: ProviderMock, payload: object, status: int
) -> None:
    providers.set(PHOTON_HOST, payload, status=status)
    response = await client.get("/api/geocode/suggest", params={"q": "Rua Abreu"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


async def test_suggest_has_its_own_quota_after_validation(
    client: httpx.AsyncClient, providers: ProviderMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "RATE_LIMIT_GEOCODE_SUGGEST_PER_MIN", 1)
    providers.set(PHOTON_HOST, photon_payload())

    assert (await client.get("/api/geocode/suggest", params={"q": " a "})).status_code == 422
    assert (await client.get("/api/geocode/suggest", params={"q": "Rua A"})).status_code == 200
    blocked = await client.get("/api/geocode/suggest", params={"q": "Rua B"})
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"

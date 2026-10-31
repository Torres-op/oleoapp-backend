from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from pymongo.errors import ServerSelectionTimeoutError

from app.core.config import settings


@pytest.fixture
async def raw_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Cliente SEM o cabeçalho X-OleoApp-Client."""
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


def _assert_no_store(response: httpx.Response) -> None:
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


async def test_post_sem_cabecalho_responde_403(raw_client: httpx.AsyncClient):
    response = await raw_client.post("/api/health")
    assert response.status_code == 403
    assert response.json() == {
        "error": {"code": "forbidden", "message": "Requisição não permitida.", "fields": None}
    }
    _assert_no_store(response)


async def test_cabecalho_com_valor_errado_responde_403(client: httpx.AsyncClient):
    response = await client.post("/api/health", headers={"X-OleoApp-Client": "mobile"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_get_nao_exige_o_cabecalho(raw_client: httpx.AsyncClient):
    response = await raw_client.get("/api/health")
    assert response.status_code == 200
    _assert_no_store(response)


async def test_post_com_cabecalho_em_rota_get_responde_405_no_formato_padrao(
    client: httpx.AsyncClient,
):
    response = await client.post("/api/health")
    assert response.status_code == 405
    assert response.json() == {
        "error": {
            "code": "method_not_allowed",
            "message": "Método não permitido.",
            "fields": None,
        }
    }
    _assert_no_store(response)


async def test_rota_inexistente_responde_404_no_formato_padrao(client: httpx.AsyncClient):
    response = await client.get("/api/nao-existe")
    assert response.status_code == 404
    assert response.json() == {
        "error": {"code": "not_found", "message": "Recurso não encontrado.", "fields": None}
    }
    _assert_no_store(response)


async def test_rate_limit_global_responde_429_com_retry_after(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "RATE_LIMIT_GLOBAL_PER_MIN", 2)

    statuses = [(await client.get("/api/nao-existe")).status_code for _ in range(3)]
    assert statuses == [404, 404, 429]

    blocked = await client.get("/api/nao-existe")
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert int(blocked.headers["retry-after"]) >= 1
    _assert_no_store(blocked)

    # Os health checks ficam fora do limite.
    assert (await client.get("/api/health")).status_code == 200
    assert (await client.get("/api/health/ready")).status_code == 200


async def test_erro_inesperado_responde_500_generico_com_no_store(
    app: FastAPI, client: httpx.AsyncClient
):
    async def boom() -> None:
        raise RuntimeError("detalhe interno secreto")

    app.add_api_route("/api/_boom", boom, methods=["GET"])

    response = await client.get("/api/_boom")
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": "internal_error",
            "message": "Erro interno. Tente novamente.",
            "fields": None,
        }
    }
    assert "secreto" not in response.text
    _assert_no_store(response)


async def test_banco_indisponivel_em_qualquer_rota_responde_503(
    app: FastAPI, client: httpx.AsyncClient
):
    async def down() -> None:
        raise ServerSelectionTimeoutError("sem servidor")

    app.add_api_route("/api/_down", down, methods=["GET"])

    response = await client.get("/api/_down")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "service_unavailable"
    _assert_no_store(response)

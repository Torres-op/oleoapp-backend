import httpx
import pytest
from fastapi import FastAPI

from app.core.config import settings
from app.main import create_app


class BrokenDatabase:
    async def command(self, *args, **kwargs):
        raise ConnectionError("mongo indisponível")


class BrokenRedis:
    def __getattr__(self, name: str):
        async def fail(*args, **kwargs):
            raise ConnectionError("redis indisponível")

        return fail


async def test_health_responde_200_sem_acessar_o_banco(client: httpx.AsyncClient):
    response = await client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ready_mostra_mongo_e_redis(client: httpx.AsyncClient):
    response = await client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mongo": "ok", "redis": "ok"}


async def test_health_continua_200_com_mongo_indisponivel(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(app.state, "db", BrokenDatabase())

    health = await client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}

    ready = await client.get("/api/health/ready")
    assert ready.status_code == 503
    assert ready.json() == {
        "error": {
            "code": "service_unavailable",
            "message": "Serviço indisponível.",
            "fields": None,
        }
    }


async def test_ready_mostra_redis_degradado_e_nao_derruba_a_api(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(app.state, "redis", BrokenRedis())

    response = await client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json()["redis"] == "degraded"

    # Rate limit falha aberto: a API segue respondendo mesmo com o Redis fora do ar.
    other = await client.get("/api/nao-existe")
    assert other.status_code == 404


async def test_app_sobe_com_redis_url_vazio(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "REDIS_URL", "")
    application = create_app()
    async with application.router.lifespan_context(application):
        assert application.state.redis is None
        transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
            response = await http_client.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "mongo": "ok", "redis": "disabled"}

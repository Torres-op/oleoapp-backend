import httpx
import pytest
from starlette.requests import Request

from app.core.config import settings
from app.core.rate_limit import client_ip

SIGNUP = {"name": "Ana Souza", "email": "ana@exemplo.com", "password": "Senha1234"}


def make_request(forwarded: str | None = None, peer: str = "10.0.0.1") -> Request:
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded is not None else []
    return Request({"type": "http", "headers": headers, "client": (peer, 1234)})


# --- client_ip --------------------------------------------------------------


def test_usa_a_entrada_mais_a_direita_do_x_forwarded_for(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)

    request = make_request("1.1.1.1, 2.2.2.2, 203.0.113.7")

    assert client_ip(request) == "203.0.113.7"


def test_entradas_forjadas_pelo_cliente_a_esquerda_sao_ignoradas(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)

    first = client_ip(make_request("9.9.9.1, 203.0.113.7"))
    second = client_ip(make_request("9.9.9.2, 203.0.113.7"))

    assert first == second == "203.0.113.7"


def test_com_dois_proxies_usa_a_penultima_entrada(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 2)

    request = make_request("9.9.9.9, 198.51.100.5, 203.0.113.7")

    assert client_ip(request) == "198.51.100.5"


def test_sem_cabecalho_usa_o_ip_da_conexao(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)

    assert client_ip(make_request(None, peer="10.0.0.9")) == "10.0.0.9"


def test_com_zero_proxies_ignora_o_cabecalho(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 0)

    request = make_request("203.0.113.7", peer="10.0.0.9")

    assert client_ip(request) == "10.0.0.9"


def test_cabecalho_com_menos_entradas_que_proxies_usa_o_ip_da_conexao(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 2)

    request = make_request("203.0.113.7", peer="10.0.0.9")

    assert client_ip(request) == "10.0.0.9"


# --- regressão: trocar o X-Forwarded-For não fura o limite de login ----------


async def test_trocar_o_ip_forjado_nao_escapa_do_limite_de_login(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1)
    await client.post("/api/auth/signup", json=SIGNUP)

    # O cliente forja um IP diferente a cada tentativa; o proxy do Render
    # acrescenta sempre o IP real à direita.
    for attempt in range(5):
        response = await client.post(
            "/api/auth/login",
            json={"email": SIGNUP["email"], "password": "SenhaErrada1"},
            headers={"X-Forwarded-For": f"9.9.9.{attempt}, 203.0.113.7"},
        )
        assert response.status_code == 401

    blocked = await client.post(
        "/api/auth/login",
        json={"email": SIGNUP["email"], "password": "SenhaErrada1"},
        headers={"X-Forwarded-For": "9.9.9.99, 203.0.113.7"},
    )
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"

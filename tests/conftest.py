import os

# As variáveis de teste precisam existir ANTES de importar o app (Settings é lido no import).
# ENV e REDIS_URL são forçados (sem setdefault): mesmo dentro do container de desenvolvimento,
# que herda o .env, os testes nunca rodam como "development" nem limpam o Redis de dev.
# Com ENV=test, o Settings também deixa de ler o arquivo .env (veja app/core/config.py).
os.environ["ENV"] = "test"
os.environ["REDIS_URL"] = os.environ.get("TEST_REDIS_URL") or "redis://localhost:6379/15"
os.environ["MONGO_DB_NAME"] = "oleoapp_test"
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")
os.environ.setdefault("EMAIL_PROVIDER", "console")

from collections.abc import AsyncIterator, Awaitable, Callable, Iterator  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
from bson import ObjectId  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from app.api.deps import CurrentUser, get_current_user, get_http_client  # noqa: E402
from app.db.indexes import ensure_indexes  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture(autouse=True)
async def _clean_state(app: FastAPI) -> None:
    """Limpa o banco de teste e o Redis antes de cada teste."""
    db = app.state.db
    assert db.name == "oleoapp_test", "Os testes só podem rodar no banco oleoapp_test."
    # Apaga coleção por coleção: o usuário da API só tem readWrite (sem dropDatabase).
    for name in await db.list_collection_names():
        await db.drop_collection(name)
    await ensure_indexes(db)
    if app.state.redis is not None:
        await app.state.redis.flushdb()


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
        headers={"X-OleoApp-Client": "web"},
    ) as http_client:
        yield http_client


@pytest.fixture
def login_as(app: FastAPI) -> Iterator[Callable[..., CurrentUser]]:
    """Faz get_current_user devolver um usuário de teste (sem passar pelo login real)."""

    def _login_as(
        user_id: str | None = None,
        *,
        name: str = "Usuário Teste",
        email: str = "teste@exemplo.com",
        reminder_interval_days: int = 180,
        role: str = "user",
    ) -> CurrentUser:
        user = CurrentUser(
            id=user_id or str(ObjectId()),
            name=name,
            email=email,
            reminder_interval_days=reminder_interval_days,
            role=role,
        )
        app.dependency_overrides[get_current_user] = lambda: user
        return user

    yield _login_as
    app.dependency_overrides.pop(get_current_user, None)


MockHandler = Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]


@pytest.fixture
async def http_mock(app: FastAPI) -> AsyncIterator[Callable[[MockHandler], httpx.AsyncClient]]:
    """Troca o cliente HTTP da aplicação por um httpx.MockTransport com o handler dado.

    Ao final, remove só esse override e fecha os clientes criados.
    """
    clients: list[httpx.AsyncClient] = []

    def install(handler: MockHandler) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        clients.append(client)
        app.dependency_overrides[get_http_client] = lambda: client
        return client

    yield install
    app.dependency_overrides.pop(get_http_client, None)
    for client in clients:
        await client.aclose()

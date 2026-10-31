import logging
import time

from fastapi import FastAPI, Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.core.config import settings
from app.core.errors import error_response, unhandled_exception_handler
from app.core.rate_limit import client_ip, hit, rate_limited_response

access_logger = logging.getLogger("oleoapp.access")

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
RATE_LIMIT_EXEMPT_PATHS = {"/api/health", "/api/health/ready"}


async def log_requests(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Registra método, path (sem query string), status e duração. Sem IP, corpo ou cookies."""
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        duration_ms = (time.perf_counter() - start) * 1000
        access_logger.info(
            "%s %s -> %s (%.1f ms)", request.method, request.url.path, status, duration_ms
        )


async def security_headers(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Cabeçalhos em toda resposta, inclusive 403/429 das etapas seguintes e erros 500."""
    try:
        response = await call_next(request)
    except Exception as exc:
        response = await unhandled_exception_handler(request, exc)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def require_client_header(request: Request, call_next: RequestResponseEndpoint) -> Response:
    path = request.url.path
    if (
        request.method in UNSAFE_METHODS
        and path.startswith("/api")
        and request.headers.get("X-OleoApp-Client") != "web"
    ):
        return error_response(403, "forbidden", "Requisição não permitida.")
    return await call_next(request)


async def global_rate_limit(request: Request, call_next: RequestResponseEndpoint) -> Response:
    if request.url.path in RATE_LIMIT_EXEMPT_PATHS:
        return await call_next(request)
    redis = getattr(request.app.state, "redis", None)
    allowed, retry_after = await hit(
        redis, f"rl:global:{client_ip(request)}", settings.RATE_LIMIT_GLOBAL_PER_MIN, 60
    )
    if not allowed:
        return rate_limited_response(retry_after)
    return await call_next(request)


def register_middlewares(app: FastAPI) -> None:
    # No Starlette, o último middleware adicionado fica por fora.
    # Ordem final (de fora para dentro): log -> cabeçalhos -> CSRF -> rate limit global.
    app.add_middleware(BaseHTTPMiddleware, dispatch=global_rate_limit)
    app.add_middleware(BaseHTTPMiddleware, dispatch=require_client_header)
    app.add_middleware(BaseHTTPMiddleware, dispatch=security_headers)
    app.add_middleware(BaseHTTPMiddleware, dispatch=log_requests)

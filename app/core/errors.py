import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pymongo.errors import ConnectionFailure
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger(__name__)


class AppError(Exception):
    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        fields: list[dict[str, str]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.fields = fields
        self.headers = headers


def error_response(
    status: int,
    code: str,
    message: str,
    fields: list[dict[str, str]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = {"error": {"code": code, "message": message, "fields": fields}}
    return JSONResponse(status_code=status, content=body, headers=headers)


# Tradução das mensagens de validação do Pydantic (seção 7.1: mensagens em pt-BR).
_VALIDATION_MESSAGES: dict[str, str] = {
    "missing": "Campo obrigatório.",
    "extra_forbidden": "Campo não permitido.",
    "string_too_short": "Use pelo menos {min_length} caracteres.",
    "string_too_long": "Use no máximo {max_length} caracteres.",
    "string_type": "Informe um texto.",
    "string_pattern_mismatch": "Formato inválido.",
    "int_parsing": "Informe um número inteiro.",
    "int_from_float": "Informe um número inteiro.",
    "int_type": "Informe um número inteiro.",
    "float_parsing": "Informe um número.",
    "float_type": "Informe um número.",
    "greater_than_equal": "O valor mínimo é {ge}.",
    "less_than_equal": "O valor máximo é {le}.",
    "literal_error": "Valor não permitido.",
    "enum": "Valor não permitido.",
    "too_short": "Informe pelo menos {min_length} item(ns).",
    "json_invalid": "JSON inválido.",
    "model_type": "Informe um objeto JSON.",
    "model_attributes_type": "Informe um objeto JSON.",
    "date_from_datetime_parsing": "Data inválida.",
    "date_parsing": "Data inválida.",
    "date_type": "Data inválida.",
}


def _translate(error: dict[str, Any]) -> str:
    message = str(error.get("msg", "Valor inválido."))
    if message.startswith("value is not a valid email address"):
        return "E-mail inválido."
    error_type = error.get("type", "")
    if error_type == "value_error":
        # Mensagens dos nossos validadores já estão em pt-BR.
        return message.removeprefix("Value error, ")
    template = _VALIDATION_MESSAGES.get(error_type)
    if template is None:
        return message
    try:
        return template.format(**(error.get("ctx") or {}))
    except (KeyError, IndexError):
        return message


def _field_path(error: dict[str, Any]) -> str:
    loc = [str(part) for part in error.get("loc", ())]
    path = ".".join(loc[1:])
    if path:
        return path
    return loc[0] if loc else ""


async def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    return error_response(exc.status, exc.code, exc.message, exc.fields, exc.headers)


async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    fields = [{"field": _field_path(err), "message": _translate(err)} for err in exc.errors()]
    return error_response(422, "validation_error", "Dados inválidos.", fields)


async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    headers = dict(exc.headers) if exc.headers else None
    if exc.status_code == 404:
        return error_response(404, "not_found", "Recurso não encontrado.", headers=headers)
    if exc.status_code == 405:
        return error_response(405, "method_not_allowed", "Método não permitido.", headers=headers)
    if exc.status_code >= 500:
        return error_response(500, "internal_error", "Erro interno. Tente novamente.")
    return error_response(exc.status_code, "bad_request", "Requisição inválida.", headers=headers)


async def database_unavailable_handler(request: Request, exc: ConnectionFailure) -> JSONResponse:
    logger.warning("Banco indisponível em %s %s: %s", request.method, request.url.path, type(exc))
    return error_response(503, "service_unavailable", "Serviço indisponível.")


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Detalhes só no log (sem query string); a resposta é genérica.
    logger.exception("Erro inesperado em %s %s", request.method, request.url.path, exc_info=exc)
    return error_response(500, "internal_error", "Erro interno. Tente novamente.")


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(ConnectionFailure, database_unavailable_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

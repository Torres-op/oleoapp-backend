"""Cria o primeiro administrador ou promove/rebaixa uma conta existente.

Uso:
    python -m scripts.create_admin [--email E-MAIL] [--name NOME]
    python -m scripts.create_admin --email E-MAIL --revoke    # volta a ser usuário comum

O e-mail vem de --email, da variável ADMIN_EMAIL ou é perguntado no terminal. A senha (só
para conta nova, ou para uma conta que entra só com o Google) vem de ADMIN_PASSWORD ou é
digitada duas vezes sem aparecer na tela. Não existe senha padrão. Usa MONGO_URL e
MONGO_DB_NAME do ambiente (no Docker: docker compose exec api python -m scripts.create_admin).

Não existe rota da API que promova alguém a admin: só este script, com acesso ao banco.
"""

import argparse
import asyncio
import getpass
import os
import sys
from datetime import UTC, datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError
from pymongo.errors import PyMongoError

from app.core.security import hash_password
from app.db.mongo import create_mongo_client, get_database
from app.repositories import users
from app.schemas.auth import Email, Name, Password
from app.services import audit
from app.services.audit import RequestContext

DEFAULT_NAME = "Administrador"
SCRIPT_CONTEXT = RequestContext(ip=None, user_agent="scripts.create_admin")

_email = TypeAdapter(Email)
_password = TypeAdapter(Password)
_name = TypeAdapter(Name)


class Abort(Exception):
    """Entrada inválida: a mensagem é impressa e o script sai com código 1."""


def _first_error(exc: ValidationError) -> str:
    return str(exc.errors()[0].get("msg", "valor inválido")).removeprefix("Value error, ")


def read_email(cli_value: str | None) -> str:
    raw = cli_value or os.environ.get("ADMIN_EMAIL") or input("E-mail do administrador: ")
    try:
        return _email.validate_python(raw.strip())
    except ValidationError as exc:
        raise Abort(f"E-mail inválido: {_first_error(exc)}") from None


def read_password(*, optional: bool = False) -> str | None:
    """Senha de ADMIN_PASSWORD ou digitada duas vezes. Vazia só é aceita se `optional`."""
    from_env = os.environ.get("ADMIN_PASSWORD")
    if from_env:
        password = from_env
    else:
        hint = " (Enter para manter só o login com Google)" if optional else ""
        password = getpass.getpass(f"Senha{hint}: ")
        if not password and optional:
            return None
        if getpass.getpass("Repita a senha: ") != password:
            raise Abort("As senhas não são iguais.")
    try:
        return _password.validate_python(password)
    except ValidationError as exc:
        raise Abort(f"Senha fraca: {_first_error(exc)}") from None


async def run(email: str, *, name: str | None = None, revoke: bool = False) -> int:
    client = create_mongo_client()
    try:
        db = get_database(client)
        document = await users.find_by_email(db, email)
        now = datetime.now(UTC)

        if revoke:
            if document is None:
                print("Conta não encontrada.")
                return 1
            await users.update_fields(db, document["_id"], {"role": "user", "updated_at": now})
            await audit.record(
                db,
                "admin_revoked",
                SCRIPT_CONTEXT,
                user_id=document["_id"],
                email=email,
                reason="script",
            )
            print(f"{email} voltou a ser usuário comum.")
            return 0

        if document is not None:
            fields: dict[str, Any] = {"role": "admin", "updated_at": now}
            if not document.get("password_hash"):
                password = read_password(optional=True)
                if password:
                    fields["password_hash"] = await asyncio.to_thread(hash_password, password)
            await users.update_fields(db, document["_id"], fields)
            user_id = document["_id"]
            print(f"{email} agora é administrador.")
        else:
            password = read_password()
            try:
                display_name = _name.validate_python(name or DEFAULT_NAME)
            except ValidationError as exc:
                raise Abort(f"Nome inválido: {_first_error(exc)}") from None
            user_id = await users.insert(
                db,
                {
                    "name": display_name,
                    "email": email,
                    "password_hash": await asyncio.to_thread(hash_password, password or ""),
                    "role": "admin",
                    "bairro": None,
                    "reminder_interval_days": 180,
                    "created_at": now,
                    "updated_at": now,
                },
            )
            print(f"Administrador {email} criado.")

        await audit.record(
            db, "admin_promoted", SCRIPT_CONTEXT, user_id=user_id, email=email, reason="script"
        )
        return 0
    except Abort as exc:
        print(exc)
        return 1
    except PyMongoError as exc:
        print(f"Falha ao acessar o banco: {type(exc).__name__}")
        return 1
    finally:
        await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Cria ou promove o administrador do ÓleoApp.")
    parser.add_argument("--email", help="e-mail da conta (ou ADMIN_EMAIL)")
    parser.add_argument("--name", help=f'nome de uma conta nova (padrão: "{DEFAULT_NAME}")')
    parser.add_argument("--revoke", action="store_true", help="tira o papel de administrador")
    args = parser.parse_args(argv)
    try:
        email = read_email(args.email)
    except Abort as exc:
        print(exc)
        return 1
    return asyncio.run(run(email, name=args.name, revoke=args.revoke))


if __name__ == "__main__":
    sys.exit(main())

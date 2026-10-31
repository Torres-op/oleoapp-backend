"""Importa pontos de coleta de um JSON versionado para o MongoDB (local ou Atlas).

Uso:
    python -m scripts.import_points <arquivo.json>            # simulação (não grava nada)
    python -m scripts.import_points <arquivo.json> --apply    # grava

Usa MONGO_URL e MONGO_DB_NAME do ambiente. Antes de qualquer coisa, imprime o host de
destino (sem usuário e senha) para evitar importar no banco errado.

Travas do --apply:
    --confirm-host <host>   obrigatório quando o destino não é local (localhost, 127.0.0.1
                            ou mongo); repita exatamente o host impresso em "Destino:".
    --allow-deactivate      obrigatório quando a gravação desativaria pontos que não estão
                            no arquivo (ex.: arquivo parcial ou o de exemplo).
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError
from pymongo.errors import PyMongoError

from app.core.config import settings
from app.db.mongo import create_mongo_client, get_database
from app.repositories import points as points_repo
from app.schemas.points import PointSeed

LOCAL_HOSTS = {"localhost", "127.0.0.1", "mongo"}


def safe_target(mongo_url: str) -> str:
    """Host (e porta) de destino, sem usuário nem senha."""
    netloc = urlsplit(mongo_url).netloc
    return netloc.rpartition("@")[2] or "(desconhecido)"


def is_local_target(target: str) -> bool:
    """True se todos os hosts do destino são locais (localhost, 127.0.0.1 ou "mongo")."""
    hosts = [part.rpartition(":")[0] or part for part in target.split(",") if part]
    return bool(hosts) and all(host.lower() in LOCAL_HOSTS for host in hosts)


def _describe(error: dict[str, Any]) -> str:
    path = ".".join(str(part) for part in error.get("loc", ()))
    message = str(error.get("msg", "")).removeprefix("Value error, ")
    return f"{path}: {message}" if path else message


def load_items(path: Path) -> list[Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("O arquivo deve conter uma lista de pontos (JSON array).")
    return data


def validate_items(items: list[Any]) -> tuple[list[PointSeed], list[str]]:
    """Valida cada item com PointSeed. Devolve (pontos válidos, erros com índice e slug)."""
    seeds: list[PointSeed] = []
    errors: list[str] = []
    first_index_by_slug: dict[str, int] = {}

    for index, item in enumerate(items):
        slug = item.get("slug") if isinstance(item, dict) else None
        label = f"[{index}] {slug or '(sem slug)'}"
        try:
            seed = PointSeed.model_validate(item)
        except ValidationError as exc:
            for error in exc.errors():
                errors.append(f"{label}: {_describe(error)}")
            continue

        if seed.slug in first_index_by_slug:
            first = first_index_by_slug[seed.slug]
            errors.append(f"{label}: slug duplicado (já usado no item [{first}])")
            continue
        first_index_by_slug[seed.slug] = index
        seeds.append(seed)

    return seeds, errors


async def run(
    path: Path,
    apply: bool,
    *,
    allow_deactivate: bool = False,
    confirm_host: str | None = None,
) -> int:
    target = safe_target(settings.MONGO_URL)
    print(f"Destino: {target} (banco: {settings.MONGO_DB_NAME})")
    print("Modo: gravação (--apply)" if apply else "Modo: simulação (use --apply para gravar)")

    if apply and not is_local_target(target) and confirm_host != target:
        print(
            "O destino não é local. Para gravar, confirme o host repetindo-o exatamente: "
            f"--confirm-host {target}"
        )
        print("Nada foi gravado.")
        return 1

    try:
        items = load_items(path)
    except (OSError, ValueError) as exc:
        print(f"Erro ao ler {path}: {exc}")
        return 1

    seeds, errors = validate_items(items)
    if not items:
        errors.append("O arquivo está vazio: nenhum ponto para importar.")

    print(f"Itens no arquivo: {len(items)}")
    if errors:
        print(f"Erros: {len(errors)}")
        for error in errors:
            print(f"  - {error}")
        print("Nada foi gravado.")
        return 1

    slugs = {seed.slug for seed in seeds}
    client = None
    writing = False
    try:
        client = create_mongo_client()
        db = get_database(client)
        existing = await points_repo.find_existing_slugs(db)
        to_deactivate = await points_repo.find_active_slugs_missing(db, slugs)
        # Pontos criados ou editados no painel de admin: o import nunca os altera nem desativa.
        managed = await points_repo.find_managed(db)
        kept = sorted(
            (slugs & managed.keys()) | {slug for slug, active in managed.items() if active}
        )
        writes = [seed for seed in seeds if seed.slug not in managed]

        print(f"Novos: {len(slugs - existing)}")
        print(f"Atualizados: {len((slugs & existing) - managed.keys())}")
        print(f"A desativar: {len(to_deactivate)}")
        print(f"Mantidos pelo admin: {len(kept)}")
        print("Erros: 0")
        for slug in to_deactivate:
            print(f"  - será desativado: {slug}")
        for slug in kept:
            print(f"  - mantido pelo admin (não muda): {slug}")

        if not apply:
            if to_deactivate:
                print("Para gravar com essas desativações, use --apply --allow-deactivate.")
            print("Simulação concluída. Nada foi gravado.")
            return 0

        if to_deactivate and not allow_deactivate:
            print(
                f"A gravação desativaria {len(to_deactivate)} ponto(s) que não estão no arquivo. "
                "Confira se o arquivo está completo; se as desativações forem intencionais, "
                "rode de novo com --allow-deactivate."
            )
            print("Nada foi gravado.")
            return 1

        now = datetime.now(UTC)
        writing = True
        for seed in writes:
            await points_repo.upsert_by_slug(db, seed, now)
        deactivated = await points_repo.deactivate_missing(db, slugs, now)
        print(f"Gravado: {len(writes)} pontos (upsert), {deactivated} desativados.")
        return 0
    except PyMongoError as exc:
        print(f"Falha ao acessar o MongoDB ({type(exc).__name__}).")
        if writing:
            print(
                "A gravação pode ter sido parcial. Rode o mesmo comando de novo: "
                "a importação é idempotente (upsert pelo slug)."
            )
        else:
            print("Nada foi gravado.")
        return 1
    finally:
        if client is not None:
            await client.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Importa pontos de coleta de um arquivo JSON.")
    parser.add_argument("file", type=Path, help="arquivo JSON (ex.: seed/collection_points.json)")
    parser.add_argument("--apply", action="store_true", help="grava no banco (padrão: simulação)")
    parser.add_argument(
        "--allow-deactivate",
        action="store_true",
        help="permite desativar os pontos ativos que não estão no arquivo",
    )
    parser.add_argument(
        "--confirm-host",
        metavar="HOST",
        help='host de destino (como impresso em "Destino:"), exigido no --apply fora do local',
    )
    args = parser.parse_args(argv)
    return asyncio.run(
        run(
            args.file,
            args.apply,
            allow_deactivate=args.allow_deactivate,
            confirm_host=args.confirm_host,
        )
    )


if __name__ == "__main__":
    sys.exit(main())

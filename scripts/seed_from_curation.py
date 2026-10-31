"""Gera o seed de pontos de coleta a partir da planilha de curadoria (CSV UTF-8).

Uso:
    python -m scripts.seed_from_curation <curadoria.csv> [--out seed/collection_points.json]
    python -m scripts.seed_from_curation <curadoria.csv> --merge   # junta com o JSON existente

Usa só as linhas com verificado = sim e pelo menos um aceita_* = sim. As colunas *_google
servem de referência para a equipe e não vão para o seed (exceto google_place_id): pelos
termos do Google, só o place_id pode ser guardado. Linha inválida não interrompe a leitura;
no fim o script lista "linha N: motivo" e, havendo erro, sai com código 1 sem gravar nada.

Não grava no MongoDB. Depois de gerar o arquivo:
    python -m scripts.import_points seed/collection_points.json            # simulação
    python -m scripts.import_points seed/collection_points.json --apply    # grava
"""

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

from pydantic import ValidationError

# O script não usa o Mongo, mas Settings exige MONGO_URL. Um padrão evita pedir essa variável.
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")

from app.schemas.points import OilType, PointSeed  # noqa: E402

DEFAULT_OUT = Path("seed/collection_points.json")
DEFAULT_SOURCE = "curadoria"

OIL_COLUMNS = {
    "aceita_motor_carro": OilType.MOTOR_CARRO,
    "aceita_motor_moto": OilType.MOTOR_MOTO,
    "aceita_motor_diesel": OilType.MOTOR_DIESEL,
    "aceita_outros_lubrificantes": OilType.OUTROS_LUBRIFICANTES,
}

YES = {"sim", "s"}
NO = {"nao", "n", ""}
CEP_DIGITS = re.compile(r"^(\d{5})-?(\d{3})$")

# Nomes dos campos do PointSeed como a equipe os conhece na planilha.
FIELD_LABELS = {
    "slug": "nome/bairro (slug)",
    "name": "nome",
    "address.street": "rua",
    "address.number": "numero",
    "address.bairro": "bairro",
    "address.city": "cidade",
    "address.state": "uf",
    "address.cep": "cep",
    "location": "lat/lng",
    "location.lat": "lat",
    "location.lng": "lng",
    "phone": "telefone",
    "opening_hours": "horario",
    "accepted_oils": "aceita_*",
    "conditions": "condicoes",
    "verified_at": "verificado_em",
    "source": "fonte",
    "google_place_id": "google_place_id",
}


def plain(text: str) -> str:
    """Minúsculas, sem acentos e sem espaços nas pontas."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def parse_yes_no(value: str | None) -> bool:
    """sim/s → True; não/nao/n/vazio → False; outro texto → ValueError."""
    text = plain(value or "")
    if text in YES:
        return True
    if text in NO:
        return False
    raise ValueError(f'use "sim" ou "não" (recebido: "{value}")')


def flag(row: dict[str, str], column: str) -> bool:
    """Valor sim/não da coluna; texto ilegível vira ValueError com o nome da coluna."""
    try:
        return parse_yes_no(row.get(column))
    except ValueError as exc:
        raise ValueError(f"{column}: {exc}") from None


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", plain(text)).strip("-")
    return slug[:80].rstrip("-")


def unique_slug(base: str, used: set[str]) -> str:
    """base, base-2, base-3... o primeiro que ainda não foi usado."""
    slug, n = base, 2
    while slug in used:
        suffix = f"-{n}"
        slug = base[: 80 - len(suffix)].rstrip("-") + suffix
        n += 1
    used.add(slug)
    return slug


def _optional(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _cep(value: str | None) -> str | None:
    text = _optional(value)
    if text is None:
        return None
    match = CEP_DIGITS.match(text.replace(".", "").replace(" ", ""))
    return f"{match[1]}-{match[2]}" if match else text  # formato errado: o PointSeed acusa


def _number(value: str | None) -> str | None:
    """Aceita vírgula decimal (padrão do Excel em português)."""
    text = _optional(value)
    return text.replace(",", ".") if text else None


def _describe(error: dict[str, Any]) -> str:
    path = ".".join(str(part) for part in error.get("loc", ()))
    field = FIELD_LABELS.get(path, path)
    message = str(error.get("msg", "")).removeprefix("Value error, ")
    return f"{field}: {message}" if field else message


def wants_row(row: dict[str, str]) -> bool:
    """Linha verificada e com pelo menos um tipo de óleo aceito."""
    try:
        return flag(row, "verificado") and any(flag(row, column) for column in OIL_COLUMNS)
    except ValueError:
        return True  # resposta ilegível: entra para o erro aparecer no relatório


def build_item(row: dict[str, str], used_slugs: set[str]) -> dict[str, Any]:
    """Monta o item do seed e valida com PointSeed. Levanta ValueError ou ValidationError."""
    if not flag(row, "verificado"):
        raise ValueError('verificado: use "sim" para pontos confirmados')
    oils = [oil.value for column, oil in OIL_COLUMNS.items() if flag(row, column)]
    name = (row.get("nome") or "").strip()
    bairro = (row.get("bairro") or "").strip()
    base = slugify(f"{name} {bairro}")
    if len(base) < 3:
        raise ValueError("nome: preencha o nome do ponto")

    item = {
        "slug": base,
        "name": name,
        "address": {
            "street": (row.get("rua") or "").strip(),
            "number": _optional(row.get("numero")),
            "bairro": bairro,
            "city": (row.get("cidade") or "").strip(),
            "state": (row.get("uf") or "").strip().upper(),
            "cep": _cep(row.get("cep")),
        },
        "location": {"lat": _number(row.get("lat")), "lng": _number(row.get("lng"))},
        "phone": _optional(row.get("telefone")),
        "opening_hours": _optional(row.get("horario")),
        "accepted_oils": oils,
        "conditions": _optional(row.get("condicoes")),
        "verified": True,
        "verified_at": _optional(row.get("verificado_em")),
        "source": _optional(row.get("fonte")) or DEFAULT_SOURCE,
        "google_place_id": _optional(row.get("google_place_id")),
        "active": True,
    }
    PointSeed.model_validate(item)  # valida antes de reservar o slug
    item["slug"] = unique_slug(base, used_slugs)
    return PointSeed.model_validate(item).model_dump(mode="json")


def read_curation(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Lê a planilha. Devolve (itens válidos do seed, erros "linha N: motivo")."""
    items: list[dict[str, Any]] = []
    errors: list[str] = []
    used_slugs: set[str] = set()

    with path.open(encoding="utf-8-sig", newline="") as file:
        # Linha 1 é o cabeçalho: a primeira linha de dados é a 2, como no Excel.
        for line, row in enumerate(csv.DictReader(file), start=2):
            if not wants_row(row):
                continue
            try:
                items.append(build_item(row, used_slugs))
            except ValidationError as exc:
                errors.extend(f"linha {line}: {_describe(error)}" for error in exc.errors())
            except ValueError as exc:
                errors.append(f"linha {line}: {exc}")
    return items, errors


def merge_items(existing: list[Any], curated: list[dict[str, Any]]) -> list[Any]:
    """Junta por slug: a curadoria substitui; os demais itens existentes ficam como estão."""
    curated_slugs = {item["slug"] for item in curated}
    kept = [
        item
        for item in existing
        if not (isinstance(item, dict) and item.get("slug") in curated_slugs)
    ]
    return kept + curated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gera o seed a partir da planilha de curadoria.")
    parser.add_argument("csv", type=Path, help="planilha de curadoria (CSV UTF-8)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="JSON de saída")
    parser.add_argument(
        "--merge", action="store_true", help="junta com o JSON de saída já existente"
    )
    args = parser.parse_args(argv)

    try:
        items, errors = read_curation(args.csv)
    except (OSError, csv.Error, UnicodeDecodeError) as exc:
        print(f"Erro ao ler {args.csv}: {exc}")
        return 1

    if errors:
        print(f"Erros: {len(errors)}")
        for error in errors:
            print(f"  - {error}")
        print("Nada foi gravado.")
        return 1

    if not items:
        # Não sobrescreve um seed existente com uma lista vazia.
        print("0 ponto(s) verificado(s) na curadoria. Nada foi gravado.")
        return 0

    output: list[Any] = items
    if args.merge and args.out.exists():
        try:
            existing = json.loads(args.out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"Erro ao ler {args.out}: {exc}")
            return 1
        if not isinstance(existing, list):
            print(f"{args.out} deve conter uma lista de pontos (JSON array).")
            return 1
        output = merge_items(existing, items)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(items)} ponto(s) verificado(s) da curadoria; {len(output)} no total.")
    print(f"Gravado em {args.out}. Próximo passo: python -m scripts.import_points {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

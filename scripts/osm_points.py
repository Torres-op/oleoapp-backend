"""Gera o seed de pontos de coleta com oficinas, postos, borracharias e autopeças do OpenStreetMap.

Busca os lugares na Overpass API (caixa em volta de Maricá), completa rua, bairro e CEP com a
busca reversa do Nominatim (1 requisição por segundo, como pede a política dele) e mantém só os
que ficam no município de Maricá. Não grava no MongoDB.

Uso:
    python -m scripts.osm_points [--out seed/collection_points.json] [--merge]

Depois:
    python -m scripts.import_points seed/collection_points.json            # simulação
    python -m scripts.import_points seed/collection_points.json --apply    # grava

Licença: dados © colaboradores do OpenStreetMap, sob a ODbL. O app precisa mostrar esse crédito.
"""

import argparse
import json
import os
import sys
import time
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

# O script não usa o Mongo, mas Settings exige MONGO_URL. Um padrão evita pedir essa variável.
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")

from app.core.config import settings  # noqa: E402
from app.schemas.points import OilType, PointSeed  # noqa: E402
from scripts.seed_from_curation import merge_items  # noqa: E402

DEFAULT_OUT = Path("seed/collection_points.json")
SOURCE = "openstreetmap"
CITY = "Maricá"

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
NOMINATIM_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"
# Caixa (sul, oeste, norte, leste) que envolve o município; o filtro fino é pelo Nominatim.
BBOX = (-23.03, -43.06, -22.83, -42.60)
NOMINATIM_INTERVAL_S = 1.1
# A Overpass pública devolve 429/502/504 quando está cheia: tenta de novo com espera.
OVERPASS_ATTEMPTS = 3
OVERPASS_RETRY_WAIT_S = 15
TIMEOUT_S = 60

# Tag do OSM -> (nome usado quando o lugar não tem nome, óleos aceitos).
CATEGORIES: dict[tuple[str, str], tuple[str, list[OilType]]] = {
    ("shop", "car_repair"): ("Oficina mecânica", [OilType.MOTOR_CARRO]),
    ("craft", "car_repair"): ("Oficina mecânica", [OilType.MOTOR_CARRO]),
    ("shop", "car_parts"): ("Autopeças", [OilType.MOTOR_CARRO]),
    ("shop", "tyres"): ("Borracharia", [OilType.MOTOR_CARRO]),
    ("amenity", "fuel"): ("Posto de combustível", [OilType.MOTOR_CARRO]),
    ("shop", "motorcycle_repair"): ("Oficina de motos", [OilType.MOTOR_MOTO]),
    ("shop", "motorcycle"): ("Loja de motos", [OilType.MOTOR_MOTO]),
}

CONDITIONS = "Ligue antes para confirmar se o local recebe óleo usado."


def overpass_query(bbox: tuple[float, float, float, float] = BBOX) -> str:
    box = ",".join(str(value) for value in bbox)
    selectors = "".join(f'nwr["{key}"="{value}"]({box});' for key, value in CATEGORIES)
    return f"[out:json][timeout:{TIMEOUT_S}];({selectors});out tags center;"


def _plain(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).casefold().strip()


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return " ".join(value.split()) or None


def category(tags: dict[str, Any]) -> tuple[str, list[OilType]] | None:
    for (key, value), info in CATEGORIES.items():
        if tags.get(key) == value:
            return info
    return None


def coordinates(element: dict[str, Any]) -> tuple[float, float] | None:
    """Nó tem lat/lon; via e relação vêm com "center" (pedido com `out center`)."""
    source = element if "lat" in element else element.get("center") or {}
    try:
        return float(source["lat"]), float(source["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def in_city(address: dict[str, Any]) -> bool:
    municipality = address.get("municipality") or address.get("city") or ""
    return isinstance(municipality, str) and _plain(municipality) == _plain(CITY)


def _bairro(address: dict[str, Any]) -> str | None:
    for key in ("suburb", "neighbourhood", "quarter", "village", "town", "city_district"):
        value = _text(address.get(key))
        if value:
            return value
    return None


def _uf(address: dict[str, Any]) -> str:
    code = address.get("ISO3166-2-lvl4")
    if isinstance(code, str) and code.startswith("BR-") and len(code) == 5:
        return code[3:]
    return "RJ"


def _cep(value: Any) -> str | None:
    digits = "".join(c for c in value if c.isdigit()) if isinstance(value, str) else ""
    return f"{digits[:5]}-{digits[5:]}" if len(digits) == 8 else None


def build_item(element: dict[str, Any], address: dict[str, Any]) -> dict[str, Any] | None:
    """Monta um item do seed. None se o lugar não serve (sem categoria, sem coordenada)."""
    tags = element.get("tags") or {}
    info = category(tags)
    point = coordinates(element)
    if info is None or point is None:
        return None
    fallback_name, oils = info
    lat, lng = point

    # As tags addr:* do próprio lugar valem mais que a busca reversa.
    bairro = _text(tags.get("addr:suburb")) or _bairro(address) or CITY
    street = _text(tags.get("addr:street")) or _text(address.get("road")) or "Rua sem nome"
    number = _text(tags.get("addr:housenumber")) or _text(address.get("house_number"))
    name = _text(tags.get("name")) or f"{fallback_name} - {bairro}"
    phone = _text(tags.get("phone")) or _text(tags.get("contact:phone"))
    hours = _text(tags.get("opening_hours"))
    return {
        "slug": f"osm-{element['type'][0]}{element['id']}",
        "name": name[:120],
        "address": {
            "street": street[:120],
            "number": number[:20] if number else None,
            "bairro": bairro[:60],
            "city": CITY,
            "state": _uf(address),
            "cep": _cep(tags.get("addr:postcode")) or _cep(address.get("postcode")),
        },
        "location": {"lat": lat, "lng": lng},
        "phone": phone[:30] if phone else None,
        "opening_hours": hours[:200] if hours else None,
        "accepted_oils": [oil.value for oil in oils],
        "conditions": CONDITIONS,
        "verified": False,
        "verified_at": None,
        "source": SOURCE,
        "google_place_id": None,
        "active": True,
    }


def fetch_elements(
    client: httpx.Client, sleep: Callable[[float], None] = time.sleep
) -> list[dict[str, Any]]:
    for attempt in range(1, OVERPASS_ATTEMPTS + 1):
        response = client.post(OVERPASS_URL, data={"data": overpass_query()})
        if response.status_code not in (429, 502, 503, 504) or attempt == OVERPASS_ATTEMPTS:
            break
        print(f"Overpass respondeu HTTP {response.status_code}; nova tentativa em instantes...")
        sleep(OVERPASS_RETRY_WAIT_S * attempt)
    response.raise_for_status()
    elements = response.json().get("elements")
    return [element for element in elements or [] if isinstance(element, dict)]


def reverse(client: httpx.Client, lat: float, lng: float) -> dict[str, Any]:
    params = {
        "format": "jsonv2",
        "lat": lat,
        "lon": lng,
        "zoom": 18,
        "addressdetails": 1,
        "accept-language": "pt-BR",
    }
    response = client.get(NOMINATIM_REVERSE_URL, params=params)
    response.raise_for_status()
    address = response.json().get("address")
    return address if isinstance(address, dict) else {}


def collect(
    client: httpx.Client, sleep: Callable[[float], None] = time.sleep
) -> tuple[list[dict[str, Any]], int]:
    """Itens válidos de Maricá e quantos lugares ficaram de fora (outra cidade ou inválidos)."""
    items: list[dict[str, Any]] = []
    skipped = 0
    for index, element in enumerate(fetch_elements(client, sleep)):
        point = coordinates(element)
        if point is None or category(element.get("tags") or {}) is None:
            skipped += 1
            continue
        if index:
            sleep(NOMINATIM_INTERVAL_S)
        address = reverse(client, *point)
        item = build_item(element, address) if in_city(address) else None
        if item is None:
            skipped += 1
            continue
        try:
            PointSeed.model_validate(item)
        except ValidationError:
            skipped += 1
            continue
        items.append(item)
    items.sort(key=lambda item: (item["address"]["bairro"], item["name"]))
    return items, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Gera o seed com lugares do OpenStreetMap.")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="JSON de saída")
    parser.add_argument(
        "--merge", action="store_true", help="junta com o JSON de saída já existente"
    )
    args = parser.parse_args(argv)

    headers = {"User-Agent": settings.NOMINATIM_USER_AGENT}
    try:
        with httpx.Client(timeout=TIMEOUT_S, headers=headers) as client:
            items, skipped = collect(client)
    except httpx.HTTPStatusError as exc:
        print(f"{exc.request.url.host} respondeu HTTP {exc.response.status_code}.")
        return 1
    except httpx.HTTPError as exc:
        print(f"Falha de rede ({type(exc).__name__}).")
        return 1

    if not items:
        print("Nenhum lugar encontrado em Maricá. Nada foi gravado.")
        return 1

    output: list[Any] = items
    if args.merge and args.out.exists():
        existing = json.loads(args.out.read_text(encoding="utf-8"))
        output = merge_items(existing if isinstance(existing, list) else [], items)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(items)} ponto(s) do OpenStreetMap ({skipped} fora de Maricá ou inválidos).")
    print(f"Arquivo: {args.out} ({len(output)} ponto(s) no total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

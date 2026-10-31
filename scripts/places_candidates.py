"""Lista candidatos a ponto de coleta em Maricá via Google Places (Text Search, API New).

Roda SÓ na máquina de quem faz a curadoria (nunca no Render). Não grava nada no MongoDB e
não baixa fotos nem avaliações. Gera um CSV UTF-8 em data/candidates/ (pasta ignorada pelo git).

Uso:
    python -m scripts.places_candidates [--out CAMINHO] [--radius-km N]

Requer GOOGLE_MAPS_API_KEY no .env. Faz até 6 termos x 3 páginas = 18 chamadas por execução.
Dica: no Excel, abra o CSV por "Dados > De Texto/CSV" (origem UTF-8) ou importe no Google Planilhas.
"""

import argparse
import csv
import os
import sys
from collections.abc import Iterable
from datetime import datetime
from math import cos, radians
from pathlib import Path
from typing import Any

import httpx

# O script não usa o Mongo, mas Settings exige MONGO_URL. Um padrão evita pedir essa variável.
os.environ.setdefault("MONGO_URL", "mongodb://localhost:27017")

from app.core.config import settings  # noqa: E402
from app.core.geo import within_area  # noqa: E402

QUERIES = (
    "troca de óleo",
    "lubrificantes",
    "posto de combustível",
    "autopeças",
    "oficina mecânica",
    "coleta de óleo lubrificante usado",
)

PLACES_URL = "https://places.googleapis.com/v1/places:searchText"
FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.location,"
    "places.types,places.googleMapsUri,nextPageToken"
)
PAGE_SIZE = 20
MAX_PAGES = 3
TIMEOUT_S = 15
KM_PER_DEGREE = 111.0

CSV_COLUMNS = (
    "google_place_id",
    "name",
    "formatted_address",
    "lat",
    "lng",
    "types",
    "queries",
    "google_maps_uri",
)


def build_rectangle(center_lat: float, center_lng: float, radius_km: float) -> dict[str, Any]:
    """Retângulo (low/high) que envolve o círculo de raio `radius_km` em torno do centro."""
    d_lat = radius_km / KM_PER_DEGREE
    d_lng = radius_km / (KM_PER_DEGREE * cos(radians(center_lat)))
    return {
        "low": {"latitude": center_lat - d_lat, "longitude": center_lng - d_lng},
        "high": {"latitude": center_lat + d_lat, "longitude": center_lng + d_lng},
    }


def _candidate_from_place(place: dict[str, Any]) -> dict[str, Any] | None:
    place_id = place.get("id")
    location = place.get("location") or {}
    if not place_id or "latitude" not in location or "longitude" not in location:
        return None
    return {
        "google_place_id": place_id,
        "name": (place.get("displayName") or {}).get("text", ""),
        "formatted_address": place.get("formattedAddress", ""),
        "lat": location["latitude"],
        "lng": location["longitude"],
        "types": list(place.get("types") or []),
        "queries": [],
        "google_maps_uri": place.get("googleMapsUri", ""),
    }


def merge_candidates(results: Iterable[tuple[str, dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Junta (termo, lugar) por place_id, acumulando os termos que encontraram cada lugar."""
    merged: dict[str, dict[str, Any]] = {}
    for term, place in results:
        candidate = _candidate_from_place(place)
        if candidate is None:
            continue
        current = merged.setdefault(candidate["google_place_id"], candidate)
        if term not in current["queries"]:
            current["queries"].append(term)
    return merged


def filter_in_area(candidates: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in candidates if within_area(item["lat"], item["lng"])]


def write_csv(candidates: Iterable[dict[str, Any]], path: Path) -> int:
    """Grava o CSV UTF-8 (types e queries separados por '|'). Devolve quantas linhas."""
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(CSV_COLUMNS)
        for item in candidates:
            writer.writerow(
                [
                    item["google_place_id"],
                    item["name"],
                    item["formatted_address"],
                    item["lat"],
                    item["lng"],
                    "|".join(item["types"]),
                    "|".join(item["queries"]),
                    item["google_maps_uri"],
                ]
            )
            count += 1
    return count


def default_output_path() -> Path:
    return Path("data/candidates") / f"candidatos-{datetime.now():%Y%m%d}.csv"


def fetch_term(
    client: httpx.Client, api_key: str, term: str, rectangle: dict[str, Any]
) -> list[dict[str, Any]]:
    """Busca um termo, seguindo nextPageToken por no máximo MAX_PAGES páginas."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    body: dict[str, Any] = {
        "textQuery": term,
        "languageCode": "pt-BR",
        "regionCode": "BR",
        "pageSize": PAGE_SIZE,
        "locationRestriction": {"rectangle": rectangle},
    }
    places: list[dict[str, Any]] = []
    for _ in range(MAX_PAGES):
        response = client.post(PLACES_URL, headers=headers, json=body)
        response.raise_for_status()
        data = response.json()
        places.extend(data.get("places", []))
        token = data.get("nextPageToken")
        if not token:
            break
        body = {**body, "pageToken": token}
    return places


def _google_error_message(response: httpx.Response) -> str:
    try:
        return str(response.json()["error"]["message"])
    except (ValueError, KeyError, TypeError):
        return response.reason_phrase


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lista candidatos a ponto de coleta (Places).")
    parser.add_argument("--out", type=Path, default=None, help="caminho do CSV de saída")
    parser.add_argument(
        "--radius-km",
        type=float,
        default=None,
        help="raio da busca em km (padrão: AREA_RADIUS_KM)",
    )
    args = parser.parse_args(argv)

    api_key = settings.GOOGLE_MAPS_API_KEY
    if not api_key:
        print("Defina GOOGLE_MAPS_API_KEY no .env")
        return 1

    radius_km = args.radius_km if args.radius_km is not None else settings.AREA_RADIUS_KM
    out_path = args.out or default_output_path()
    rectangle = build_rectangle(settings.AREA_CENTER_LAT, settings.AREA_CENTER_LNG, radius_km)

    results: list[tuple[str, dict[str, Any]]] = []
    try:
        with httpx.Client(timeout=TIMEOUT_S) as client:
            for term in QUERIES:
                places = fetch_term(client, api_key, term, rectangle)
                print(f"{term!r}: {len(places)} resultado(s)")
                results.extend((term, place) for place in places)
    except httpx.HTTPStatusError as exc:
        # Nunca imprimir a chave: mostramos só o status e a mensagem devolvida pelo Google.
        status = exc.response.status_code
        print(f"O Google respondeu {status}: {_google_error_message(exc.response)}")
        if status in (400, 403):
            print("Verifique se a Places API (New) está liberada para a chave (tarefa G-03).")
        return 1
    except httpx.HTTPError as exc:
        print(f"Falha de rede ao chamar o Google ({type(exc).__name__}).")
        return 1

    candidates = filter_in_area(merge_candidates(results).values())
    written = write_csv(candidates, out_path)
    print(f"{written} candidato(s) gravado(s) em {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

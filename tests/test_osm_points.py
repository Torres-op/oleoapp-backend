import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.schemas.points import PointSeed
from scripts import osm_points
from scripts.import_points import load_items, validate_items

MARICA_ADDRESS = {
    "road": "Rua Abreu Rangel",
    "house_number": "120",
    "suburb": "Centro",
    "city": "Maricá",
    "municipality": "Maricá",
    "ISO3166-2-lvl4": "BR-RJ",
    "postcode": "24900890",
}
NITEROI_ADDRESS = {"road": "Rua X", "suburb": "Icaraí", "city": "Niterói"}


def node(osm_id: int, lat: float = -22.9181, lon: float = -42.8185, **tags: str) -> dict:
    return {"type": "node", "id": osm_id, "lat": lat, "lon": lon, "tags": tags}


def client_for(elements: list[dict], addresses: dict[float, dict]) -> tuple[httpx.Client, list]:
    """Overpass devolve `elements`; o Nominatim responde pelo valor de lat da consulta."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "overpass-api.de":
            return httpx.Response(200, json={"elements": elements})
        lat = float(request.url.params["lat"])
        return httpx.Response(200, json={"address": addresses.get(lat, {})})

    return httpx.Client(transport=httpx.MockTransport(handler)), requests


def test_build_item_uses_osm_tags_first() -> None:
    element = node(
        1,
        **{
            "shop": "car_repair",
            "name": "Oficina do Zé",
            "addr:street": "Rua da Tag",
            "addr:housenumber": "7",
            "phone": "+55 21 99999-0000",
            "opening_hours": "Mo-Fr 08:00-18:00",
        },
    )
    item = osm_points.build_item(element, MARICA_ADDRESS)

    assert item is not None
    seed = PointSeed.model_validate(item)
    assert seed.slug == "osm-n1"
    assert seed.name == "Oficina do Zé"
    assert seed.address.street == "Rua da Tag"
    assert seed.address.number == "7"
    assert seed.address.bairro == "Centro"
    assert seed.address.cep == "24900-890"
    assert seed.address.state == "RJ"
    assert seed.phone == "+55 21 99999-0000"
    assert seed.accepted_oils == ["motor_carro"]
    assert seed.verified is False
    assert seed.source == "openstreetmap"


def test_build_item_without_name_uses_category_and_bairro() -> None:
    item = osm_points.build_item(node(2, shop="motorcycle_repair"), MARICA_ADDRESS)
    assert item is not None
    assert item["name"] == "Oficina de motos - Centro"
    assert item["accepted_oils"] == ["motor_moto"]
    assert item["address"]["street"] == "Rua Abreu Rangel"
    assert item["address"]["number"] == "120"


def test_build_item_for_way_uses_center() -> None:
    way = {
        "type": "way",
        "id": 3,
        "center": {"lat": -22.92, "lon": -42.82},
        "tags": {"amenity": "fuel"},
    }
    item = osm_points.build_item(way, MARICA_ADDRESS)
    assert item is not None
    assert item["slug"] == "osm-w3"
    assert item["location"] == {"lat": -22.92, "lng": -42.82}


@pytest.mark.parametrize(
    "element",
    [node(4, shop="bakery"), {"type": "way", "id": 5, "tags": {"shop": "tyres"}}],
)
def test_build_item_rejects_other_categories_and_missing_coordinates(element: dict) -> None:
    assert osm_points.build_item(element, MARICA_ADDRESS) is None


def test_collect_keeps_only_marica_and_respects_nominatim_interval() -> None:
    elements = [
        node(10, lat=-22.91, shop="car_repair", name="Oficina B"),
        node(11, lat=-22.88, amenity="fuel", name="Posto Niterói"),
        node(12, lat=-22.92, shop="tyres", name="Oficina A"),
        node(13, lat=-22.93, shop="bakery", name="Padaria"),  # categoria fora da lista
    ]
    addresses = {-22.91: MARICA_ADDRESS, -22.88: NITEROI_ADDRESS, -22.92: MARICA_ADDRESS}
    client, requests = client_for(elements, addresses)
    pauses: list[float] = []

    items, skipped = osm_points.collect(client, sleep=pauses.append)

    # Ordenados por bairro e nome.
    assert [item["name"] for item in items] == ["Oficina A", "Oficina B"]
    assert skipped == 2
    reverse_calls = [r for r in requests if r.url.host == "nominatim.openstreetmap.org"]
    assert len(reverse_calls) == 3  # a padaria nem chega ao Nominatim
    assert pauses and all(p >= 1 for p in pauses)


def test_overpass_query_lists_every_category() -> None:
    query = osm_points.overpass_query()
    for key, value in osm_points.CATEGORIES:
        assert f'nwr["{key}"="{value}"]' in query
    assert "out tags center" in query


def test_main_writes_seed_accepted_by_import_points(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = osm_points.build_item(node(20, shop="car_parts", name="Peças"), MARICA_ADDRESS)
    monkeypatch.setattr(osm_points, "collect", lambda client: ([item], 0))
    out = tmp_path / "seed.json"

    assert osm_points.main(["--out", str(out)]) == 0

    seeds, errors = validate_items(load_items(out))
    assert errors == []
    assert [seed.slug for seed in seeds] == ["osm-n20"]


def test_main_merge_keeps_existing_points(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    item = osm_points.build_item(node(21, shop="car_repair", name="Nova"), MARICA_ADDRESS)
    monkeypatch.setattr(osm_points, "collect", lambda client: ([item], 0))
    out = tmp_path / "seed.json"
    existing: list[dict[str, Any]] = [{**item, "slug": "curadoria-x", "name": "Da curadoria"}]
    out.write_text(json.dumps(existing), encoding="utf-8")

    assert osm_points.main(["--out", str(out), "--merge"]) == 0

    slugs = sorted(entry["slug"] for entry in json.loads(out.read_text(encoding="utf-8")))
    assert slugs == ["curadoria-x", "osm-n21"]


def test_main_without_results_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(osm_points, "collect", lambda client: ([], 3))
    out = tmp_path / "seed.json"
    assert osm_points.main(["--out", str(out)]) == 1
    assert not out.exists()


def test_overpass_busy_is_retried() -> None:
    statuses = iter([504, 200])

    def handler(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        return httpx.Response(status, json={"elements": [node(30, shop="tyres")]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    pauses: list[float] = []

    elements = osm_points.fetch_elements(client, sleep=pauses.append)

    assert [element["id"] for element in elements] == [30]
    assert len(pauses) == 1

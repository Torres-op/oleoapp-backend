import csv
import json
from math import cos, radians
from pathlib import Path

import httpx
import pytest

from app.core.config import settings
from scripts import places_candidates as pc

CENTER = (settings.AREA_CENTER_LAT, settings.AREA_CENTER_LNG)


def place(place_id: str, name: str, lat: float, lng: float, **extra) -> dict:
    data = {
        "id": place_id,
        "displayName": {"text": name, "languageCode": "pt-BR"},
        "formattedAddress": f"Rua {name}, Maricá - RJ",
        "location": {"latitude": lat, "longitude": lng},
        "types": ["gas_station", "point_of_interest"],
        "googleMapsUri": f"https://maps.google.com/?cid={place_id}",
    }
    data.update(extra)
    return data


# --- build_rectangle --------------------------------------------------------


def test_build_rectangle_envolve_o_centro_com_o_raio_pedido():
    rectangle = pc.build_rectangle(CENTER[0], CENTER[1], 40)

    low, high = rectangle["low"], rectangle["high"]
    assert low["latitude"] < CENTER[0] < high["latitude"]
    assert low["longitude"] < CENTER[1] < high["longitude"]
    assert high["latitude"] - CENTER[0] == pytest.approx(40 / 111)
    assert CENTER[0] - low["latitude"] == pytest.approx(40 / 111)
    expected_d_lng = 40 / (111 * cos(radians(CENTER[0])))
    assert high["longitude"] - CENTER[1] == pytest.approx(expected_d_lng)
    assert CENTER[1] - low["longitude"] == pytest.approx(expected_d_lng)


def test_build_rectangle_cresce_com_o_raio():
    small = pc.build_rectangle(CENTER[0], CENTER[1], 5)
    large = pc.build_rectangle(CENTER[0], CENTER[1], 40)
    assert large["high"]["latitude"] > small["high"]["latitude"]
    assert large["low"]["longitude"] < small["low"]["longitude"]


# --- merge_candidates -------------------------------------------------------


def test_merge_candidates_remove_duplicados_juntando_os_termos():
    results = [
        ("troca de óleo", place("p1", "Posto A", -22.92, -42.82)),
        ("lubrificantes", place("p1", "Posto A", -22.92, -42.82)),
        ("lubrificantes", place("p2", "Autopeças B", -22.93, -42.83)),
        ("troca de óleo", place("p1", "Posto A", -22.92, -42.82)),
    ]

    merged = pc.merge_candidates(results)

    assert list(merged) == ["p1", "p2"]
    assert merged["p1"]["queries"] == ["troca de óleo", "lubrificantes"]
    assert merged["p2"]["queries"] == ["lubrificantes"]
    assert merged["p1"]["name"] == "Posto A"
    assert merged["p1"]["lat"] == -22.92
    assert merged["p1"]["lng"] == -42.82
    assert merged["p1"]["types"] == ["gas_station", "point_of_interest"]
    assert merged["p1"]["google_maps_uri"] == "https://maps.google.com/?cid=p1"


def test_merge_candidates_ignora_lugares_sem_id_ou_sem_localizacao():
    results = [
        ("x", {"displayName": {"text": "Sem id"}, "location": {"latitude": 1, "longitude": 2}}),
        ("x", {"id": "p3", "displayName": {"text": "Sem local"}}),
        ("x", {"id": "p4", "location": {"latitude": -22.9}}),
        ("x", place("p5", "Válido", -22.9, -42.8)),
    ]
    assert list(pc.merge_candidates(results)) == ["p5"]


def test_merge_candidates_tolera_campos_opcionais_ausentes():
    minimal = {"id": "p6", "location": {"latitude": -22.9, "longitude": -42.8}}
    merged = pc.merge_candidates([("termo", minimal)])
    assert merged["p6"]["name"] == ""
    assert merged["p6"]["formatted_address"] == ""
    assert merged["p6"]["types"] == []
    assert merged["p6"]["google_maps_uri"] == ""


# --- filter_in_area ---------------------------------------------------------


def test_filter_in_area_mantem_so_o_que_esta_dentro_do_raio():
    candidates = [
        {"google_place_id": "centro", "lat": CENTER[0], "lng": CENTER[1]},
        {"google_place_id": "perto", "lat": CENTER[0] + 0.1, "lng": CENTER[1]},
        {"google_place_id": "sao-paulo", "lat": -23.5505, "lng": -46.6333},
        {"google_place_id": "cabo-frio", "lat": -22.879, "lng": -42.019},
    ]

    kept = pc.filter_in_area(candidates)

    assert [item["google_place_id"] for item in kept] == ["centro", "perto"]


# --- CSV --------------------------------------------------------------------


def test_write_csv_usa_as_colunas_do_contrato_e_separa_listas_com_barra(tmp_path: Path):
    merged = pc.merge_candidates(
        [
            ("troca de óleo", place("p1", "Posto São João", -22.92, -42.82)),
            ("lubrificantes", place("p1", "Posto São João", -22.92, -42.82)),
        ]
    )
    path = tmp_path / "sub" / "pasta" / "candidatos.csv"

    written = pc.write_csv(merged.values(), path)

    assert written == 1
    with path.open(encoding="utf-8", newline="") as file:
        rows = list(csv.reader(file))
    assert rows[0] == [
        "google_place_id",
        "name",
        "formatted_address",
        "lat",
        "lng",
        "types",
        "queries",
        "google_maps_uri",
    ]
    assert rows[1][0] == "p1"
    assert rows[1][1] == "Posto São João"
    assert rows[1][5] == "gas_station|point_of_interest"
    assert rows[1][6] == "troca de óleo|lubrificantes"


def test_caminho_padrao_do_csv_tem_a_data_e_fica_em_data():
    path = pc.default_output_path()
    assert path.parent == Path("data/candidates")
    assert path.name.startswith("candidatos-")
    assert path.suffix == ".csv"
    assert len(path.stem) == len("candidatos-AAAAMMDD")


# --- fetch_term (sem rede: httpx.MockTransport) -----------------------------


def test_fetch_term_segue_as_paginas_ate_o_limite_de_tres():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page = len(requests)
        found = [place(f"p{page}", f"Lugar {page}", -22.9, -42.8)]
        return httpx.Response(200, json={"places": found, "nextPageToken": "t"})

    rectangle = pc.build_rectangle(CENTER[0], CENTER[1], 40)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        places = pc.fetch_term(client, "chave-falsa", "autopeças", rectangle)

    assert len(requests) == pc.MAX_PAGES == 3
    assert [item["id"] for item in places] == ["p1", "p2", "p3"]

    first = json.loads(requests[0].content)
    assert first["textQuery"] == "autopeças"
    assert first["languageCode"] == "pt-BR"
    assert first["regionCode"] == "BR"
    assert first["pageSize"] == 20
    assert first["locationRestriction"] == {"rectangle": rectangle}
    assert "pageToken" not in first
    assert json.loads(requests[1].content)["pageToken"] == "t"

    assert str(requests[0].url) == "https://places.googleapis.com/v1/places:searchText"
    assert requests[0].headers["x-goog-api-key"] == "chave-falsa"
    assert requests[0].headers["x-goog-fieldmask"] == pc.FIELD_MASK
    assert "places.googleMapsUri" in pc.FIELD_MASK
    assert "nextPageToken" in pc.FIELD_MASK


def test_fetch_term_para_quando_nao_ha_proxima_pagina():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"places": [place("p1", "Único", -22.9, -42.8)]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        places = pc.fetch_term(client, "k", "lubrificantes", {})

    assert calls == 1
    assert len(places) == 1


def test_fetch_term_sem_resultados_devolve_lista_vazia():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert pc.fetch_term(client, "k", "oficina mecânica", {}) == []


# --- main (sem rede) --------------------------------------------------------


def test_main_sem_chave_avisa_e_sai_com_codigo_1(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", "")
    out = tmp_path / "saida.csv"

    assert pc.main(["--out", str(out)]) == 1

    assert "Defina GOOGLE_MAPS_API_KEY no .env" in capsys.readouterr().out
    assert not out.exists()


def test_main_gera_o_csv_filtrado_e_nunca_imprime_a_chave(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", "CHAVE-SECRETA-123")

    def handler(request: httpx.Request) -> httpx.Response:
        term = json.loads(request.content)["textQuery"]
        places = [
            place("dentro", "Posto Dentro", -22.92, -42.82),
            place("fora", "Posto Fora", -23.5505, -46.6333),
        ]
        if term == "lubrificantes":
            places.append(place("so-lubrificantes", "Loja", -22.93, -42.83))
        return httpx.Response(200, json={"places": places})

    real_client = httpx.Client
    monkeypatch.setattr(
        pc.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    out = tmp_path / "candidatos.csv"

    assert pc.main(["--out", str(out)]) == 0

    with out.open(encoding="utf-8", newline="") as file:
        rows = list(csv.DictReader(file))
    assert [row["google_place_id"] for row in rows] == ["dentro", "so-lubrificantes"]
    assert rows[0]["queries"].split("|") == list(pc.QUERIES)
    assert rows[1]["queries"] == "lubrificantes"
    output = capsys.readouterr().out
    assert "2 candidato(s) gravado(s)" in output
    assert "CHAVE-SECRETA-123" not in output


def test_main_com_erro_do_google_mostra_a_mensagem_sem_a_chave(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
):
    monkeypatch.setattr(settings, "GOOGLE_MAPS_API_KEY", "CHAVE-SECRETA-123")

    def handler(request: httpx.Request) -> httpx.Response:
        body = {"error": {"code": 403, "message": "Places API (New) has not been enabled."}}
        return httpx.Response(403, json=body)

    real_client = httpx.Client
    monkeypatch.setattr(
        pc.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )
    out = tmp_path / "candidatos.csv"

    assert pc.main(["--out", str(out)]) == 1

    output = capsys.readouterr().out
    assert "403" in output
    assert "has not been enabled" in output
    assert "CHAVE-SECRETA-123" not in output
    assert not out.exists()

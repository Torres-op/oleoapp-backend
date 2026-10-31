import csv
import json
from pathlib import Path

import pytest

from scripts.import_points import load_items, validate_items
from scripts.seed_from_curation import main, parse_yes_no, slugify

COLUMNS = (
    "prioridade",
    "google_place_id",
    "nome_google",
    "endereco_google",
    "google_maps_uri",
    "verificado",
    "verificado_em",
    "nome",
    "rua",
    "numero",
    "bairro",
    "cidade",
    "uf",
    "cep",
    "lat",
    "lng",
    "telefone",
    "horario",
    "aceita_motor_carro",
    "aceita_motor_moto",
    "aceita_motor_diesel",
    "aceita_outros_lubrificantes",
    "condicoes",
    "fonte",
    "observacoes",
)


def row(**overrides: str) -> dict[str, str]:
    """Linha verificada e válida, no centro de Maricá."""
    base = {
        "prioridade": "1",
        "google_place_id": "ChIJ-teste",
        "nome_google": "Nome no Google",
        "endereco_google": "Endereço no Google",
        "google_maps_uri": "https://maps.google.com/?cid=1",
        "verificado": "sim",
        "verificado_em": "2026-10-30",
        "nome": "Oficina São José",
        "rua": "Rua Abreu Rangel",
        "numero": "120",
        "bairro": "Centro",
        "cidade": "Maricá",
        "uf": "rj",
        "cep": "24900890",
        "lat": "-22,9181",
        "lng": "-42.8185",
        "telefone": "(21) 2637-0000",
        "horario": "Seg a Sex, 8h às 18h",
        "aceita_motor_carro": "Sim",
        "aceita_motor_moto": "não",
        "aceita_motor_diesel": "S",
        "aceita_outros_lubrificantes": "",
        "condicoes": "Levar em galão fechado",
        "fonte": "",
        "observacoes": "falei com o dono",
    }
    return {**base, **overrides}


def write_csv(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    path = tmp_path / "curadoria.csv"
    with path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def run(tmp_path: Path, rows: list[dict[str, str]], *extra: str) -> tuple[int, Path]:
    out = tmp_path / "seed.json"
    code = main([str(write_csv(tmp_path, rows)), "--out", str(out), *extra])
    return code, out


def read(out: Path) -> list[dict]:
    return json.loads(out.read_text(encoding="utf-8"))


def test_linha_valida_vira_ponto_do_seed(tmp_path: Path):
    code, out = run(tmp_path, [row()])

    assert code == 0
    assert read(out) == [
        {
            "slug": "oficina-sao-jose-centro",
            "name": "Oficina São José",
            "address": {
                "street": "Rua Abreu Rangel",
                "number": "120",
                "bairro": "Centro",
                "city": "Maricá",
                "state": "RJ",
                "cep": "24900-890",
            },
            "location": {"lat": -22.9181, "lng": -42.8185},
            "phone": "(21) 2637-0000",
            "opening_hours": "Seg a Sex, 8h às 18h",
            "accepted_oils": ["motor_carro", "motor_diesel"],
            "conditions": "Levar em galão fechado",
            "verified": True,
            "verified_at": "2026-10-30",
            "source": "curadoria",
            "google_place_id": "ChIJ-teste",
            "active": True,
        }
    ]


def test_colunas_do_google_nao_vao_para_o_seed(tmp_path: Path):
    _, out = run(tmp_path, [row()])

    text = out.read_text(encoding="utf-8")
    assert "Nome no Google" not in text
    assert "Endereço no Google" not in text
    assert "maps.google.com" not in text


def test_linha_nao_verificada_e_ignorada(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    code, out = run(tmp_path, [row(verificado="não"), row(verificado="")])

    assert code == 0
    assert not out.exists()  # nada verificado: não grava (nem sobrescreve) o seed
    assert "0 ponto(s)" in capsys.readouterr().out


def test_linha_sem_nenhum_tipo_de_oleo_e_ignorada(tmp_path: Path):
    no_oils = row(
        aceita_motor_carro="nao",
        aceita_motor_moto="",
        aceita_motor_diesel="N",
        aceita_outros_lubrificantes="não",
    )
    code, out = run(tmp_path, [no_oils])

    assert code == 0
    assert not out.exists()


def test_nenhum_ponto_nao_sobrescreve_o_seed_existente(tmp_path: Path):
    out = tmp_path / "seed.json"
    out.write_text(json.dumps([{"slug": "ponto-antigo"}]), encoding="utf-8")

    code = main([str(write_csv(tmp_path, [row(verificado="")])), "--out", str(out)])

    assert code == 0
    assert read(out) == [{"slug": "ponto-antigo"}]


def test_slug_repetido_ganha_sufixo(tmp_path: Path):
    code, out = run(tmp_path, [row(), row(google_place_id="b"), row(google_place_id="c")])

    assert code == 0
    assert [item["slug"] for item in read(out)] == [
        "oficina-sao-jose-centro",
        "oficina-sao-jose-centro-2",
        "oficina-sao-jose-centro-3",
    ]


def test_fonte_preenchida_e_usada(tmp_path: Path):
    _, out = run(tmp_path, [row(fonte="visita")])
    assert read(out)[0]["source"] == "visita"


def test_erros_de_validacao_sao_listados_e_nada_e_gravado(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    rows = [
        row(),
        row(nome="Fora", lat="-23.5505", lng="-46.6333"),  # São Paulo
        row(nome="UF errada", uf="Rio"),
        row(nome="Resposta ilegível", aceita_motor_moto="talvez"),
    ]
    code, out = run(tmp_path, rows)

    assert code == 1
    assert not out.exists()
    output = capsys.readouterr().out
    assert "linha 3: lat/lng:" in output
    assert "fora da área atendida" in output
    assert "linha 4: uf:" in output
    assert 'linha 5: aceita_motor_moto: use "sim" ou "não"' in output
    assert "linha 2" not in output
    assert "Nada foi gravado." in output


def test_merge_preserva_os_pontos_que_nao_estao_na_curadoria(tmp_path: Path):
    out = tmp_path / "seed.json"
    existing = [
        {"slug": "ponto-antigo", "name": "Antigo"},
        {"slug": "oficina-sao-jose-centro", "name": "Versão velha"},
    ]
    out.write_text(json.dumps(existing), encoding="utf-8")

    code = main([str(write_csv(tmp_path, [row()])), "--out", str(out), "--merge"])

    assert code == 0
    items = read(out)
    assert [item["slug"] for item in items] == ["ponto-antigo", "oficina-sao-jose-centro"]
    assert items[1]["name"] == "Oficina São José"


def test_sem_merge_substitui_o_arquivo(tmp_path: Path):
    out = tmp_path / "seed.json"
    out.write_text(json.dumps([{"slug": "ponto-antigo"}]), encoding="utf-8")

    main([str(write_csv(tmp_path, [row()])), "--out", str(out)])

    assert [item["slug"] for item in read(out)] == ["oficina-sao-jose-centro"]


def test_o_json_gerado_e_aceito_pelo_import_points(tmp_path: Path):
    _, out = run(tmp_path, [row(), row(nome="Posto Itaipuaçu", bairro="Itaipuaçu", numero="")])

    seeds, errors = validate_items(load_items(out))

    assert errors == []
    assert [seed.slug for seed in seeds] == ["oficina-sao-jose-centro", "posto-itaipuacu-itaipuacu"]
    assert seeds[1].address.number is None


def test_csv_salvo_pelo_excel_com_bom_e_lido(tmp_path: Path):
    path = write_csv(tmp_path, [row()])
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    out = tmp_path / "seed.json"

    assert main([str(path), "--out", str(out)]) == 0
    assert len(read(out)) == 1


@pytest.mark.parametrize(
    ("value", "expected"),
    [("sim", True), ("SIM", True), ("s", True), ("Não", False), ("nao", False), ("", False)],
)
def test_parse_yes_no(value: str, expected: bool):
    assert parse_yes_no(value) is expected


def test_slugify_tira_acentos_e_simbolos():
    assert slugify("  Auto Peças Nª 1 / Inoã ") == "auto-pecas-na-1-inoa"

import json
from copy import deepcopy
from pathlib import Path

import pytest
from fastapi import FastAPI
from pymongo.errors import AutoReconnect, ConfigurationError

from app.core.config import settings
from app.db.collections import POINTS
from app.repositories import points as points_repo
from scripts import import_points
from scripts.import_points import is_local_target, run, safe_target, validate_items
from tests.factories import point_seed_data as point

EXAMPLE_FILE = Path(__file__).resolve().parent.parent / "seed" / "collection_points.example.json"


def write_seed(tmp_path: Path, items: list) -> Path:
    path = tmp_path / "pontos.json"
    path.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    return path


async def count(app: FastAPI, query: dict | None = None) -> int:
    return await app.state.db[POINTS].count_documents(query or {})


async def apply(path: Path, **kwargs) -> int:
    """--apply confirmando o host do banco de teste (que pode não ser localhost no Docker)."""
    kwargs.setdefault("confirm_host", safe_target(settings.MONGO_URL))
    return await run(path, apply=True, **kwargs)


# --- simulação e gravação ---------------------------------------------------


async def test_simulacao_nao_grava_nada(
    app: FastAPI, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    path = write_seed(tmp_path, [point("ponto-a"), point("ponto-b")])

    code = await run(path, apply=False)

    assert code == 0
    assert await count(app) == 0
    output = capsys.readouterr().out
    assert "Modo: simulação" in output
    assert "Novos: 2" in output
    assert "Atualizados: 0" in output
    assert "A desativar: 0" in output
    assert "Erros: 0" in output


async def test_apply_insere_os_pontos(app: FastAPI, tmp_path: Path):
    path = write_seed(tmp_path, [point("ponto-a"), point("ponto-b")])

    assert await apply(path) == 0

    assert await count(app) == 2
    document = await app.state.db[POINTS].find_one({"slug": "ponto-a"})
    assert document["location"] == {"type": "Point", "coordinates": [-42.8186, -22.9189]}
    assert document["accepted_oils"] == ["motor_carro"]
    assert document["verified_at"].year == 2026
    assert document["active"] is True
    assert document["created_at"] is not None
    assert document["updated_at"] is not None


async def test_apply_atualiza_sem_mexer_no_created_at(app: FastAPI, tmp_path: Path):
    path = write_seed(tmp_path, [point("ponto-a")])
    await apply(path)
    before = await app.state.db[POINTS].find_one({"slug": "ponto-a"})

    path = write_seed(tmp_path, [point("ponto-a", name="EXEMPLO - novo nome")])
    code = await apply(path)

    assert code == 0
    assert await count(app) == 1
    after = await app.state.db[POINTS].find_one({"slug": "ponto-a"})
    assert after["_id"] == before["_id"]
    assert after["name"] == "EXEMPLO - novo nome"
    assert after["created_at"] == before["created_at"]
    assert after["updated_at"] >= before["updated_at"]


async def test_simulacao_conta_atualizados_e_a_desativar(
    app: FastAPI, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    await apply(write_seed(tmp_path, [point("ponto-a"), point("ponto-b")]))
    capsys.readouterr()

    path = write_seed(tmp_path, [point("ponto-a"), point("ponto-c")])
    assert await run(path, apply=False) == 0

    output = capsys.readouterr().out
    assert "Novos: 1" in output
    assert "Atualizados: 1" in output
    assert "A desativar: 1" in output
    assert "ponto-b" in output
    # Simulação: nada mudou.
    assert await count(app) == 2
    assert await count(app, {"active": False}) == 0


async def test_apply_desativa_os_slugs_que_nao_estao_no_arquivo(app: FastAPI, tmp_path: Path):
    await apply(write_seed(tmp_path, [point("ponto-a"), point("ponto-b")]))

    path = write_seed(tmp_path, [point("ponto-a")])
    assert await apply(path, allow_deactivate=True) == 0

    assert await count(app) == 2  # pontos nunca são apagados
    removed = await app.state.db[POINTS].find_one({"slug": "ponto-b"})
    kept = await app.state.db[POINTS].find_one({"slug": "ponto-a"})
    assert removed["active"] is False
    assert kept["active"] is True


async def test_apply_reativa_um_ponto_que_volta_ao_arquivo(app: FastAPI, tmp_path: Path):
    await apply(write_seed(tmp_path, [point("ponto-a"), point("ponto-b")]))
    await apply(write_seed(tmp_path, [point("ponto-a")]), allow_deactivate=True)

    await apply(write_seed(tmp_path, [point("ponto-a"), point("ponto-b")]))

    assert await count(app, {"active": True}) == 2


async def test_o_arquivo_de_exemplo_e_valido(app: FastAPI, capsys: pytest.CaptureFixture[str]):
    assert await run(EXAMPLE_FILE, apply=False) == 0
    output = capsys.readouterr().out
    assert "Novos: 2" in output
    assert "Erros: 0" in output


# --- travas do --apply ------------------------------------------------------


async def test_arquivo_parcial_sem_allow_deactivate_nao_grava_nada(
    app: FastAPI, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    # Ex.: o arquivo de exemplo ou um arquivo pela metade aplicado no banco real.
    await apply(write_seed(tmp_path, [point("ponto-a"), point("ponto-b"), point("ponto-c")]))
    capsys.readouterr()

    path = write_seed(tmp_path, [point("ponto-a", name="EXEMPLO - mudou"), point("ponto-novo")])
    code = await apply(path)

    assert code == 1
    assert await count(app) == 3
    assert await count(app, {"active": True}) == 3
    assert await count(app, {"name": "EXEMPLO - mudou"}) == 0
    output = capsys.readouterr().out
    assert "A desativar: 2" in output
    assert "--allow-deactivate" in output
    assert "Nada foi gravado." in output


async def test_simulacao_avisa_que_as_desativacoes_exigem_allow_deactivate(
    app: FastAPI, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    await apply(write_seed(tmp_path, [point("ponto-a"), point("ponto-b")]))
    capsys.readouterr()

    assert await run(write_seed(tmp_path, [point("ponto-a")]), apply=False) == 0

    assert "--apply --allow-deactivate" in capsys.readouterr().out


async def test_destino_remoto_exige_confirm_host_igual_ao_impresso(
    app: FastAPI,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(
        settings, "MONGO_URL", "mongodb+srv://u:senha@cluster0.abcde.mongodb.net/?x=1"
    )

    def no_connection():
        raise AssertionError("não deveria conectar sem a confirmação do host")

    monkeypatch.setattr(import_points, "create_mongo_client", no_connection)
    path = write_seed(tmp_path, [point("ponto-a")])

    assert await run(path, apply=True) == 1
    output = capsys.readouterr().out
    assert "--confirm-host cluster0.abcde.mongodb.net" in output
    assert "Nada foi gravado." in output

    assert await run(path, apply=True, confirm_host="outro.mongodb.net") == 1
    assert "--confirm-host cluster0.abcde.mongodb.net" in capsys.readouterr().out

    # A simulação não exige a confirmação (aqui ela só para por não poder conectar).
    with pytest.raises(AssertionError):
        await run(path, apply=False)


def test_is_local_target():
    assert is_local_target("localhost:27017")
    assert is_local_target("127.0.0.1")
    assert is_local_target("mongo:27017")
    assert is_local_target("localhost:27017,127.0.0.1:27018")
    assert not is_local_target("cluster0.abcde.mongodb.net")
    assert not is_local_target("localhost:27017,cluster0.abcde.mongodb.net")
    assert not is_local_target("(desconhecido)")


# --- falhas do MongoDB ------------------------------------------------------


async def test_falha_ao_criar_o_cliente_sai_com_codigo_1(
    app: FastAPI,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    def broken_client():
        raise ConfigurationError("URL inválida")

    monkeypatch.setattr(import_points, "create_mongo_client", broken_client)

    code = await apply(write_seed(tmp_path, [point("ponto-a")]))

    assert code == 1
    output = capsys.readouterr().out
    assert "Falha ao acessar o MongoDB (ConfigurationError)" in output
    assert "Nada foi gravado." in output


async def test_falha_no_meio_da_gravacao_avisa_que_pode_ser_parcial(
    app: FastAPI,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
):
    original = points_repo.upsert_by_slug
    calls = 0

    async def flaky_upsert(db, seed, now=None):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise AutoReconnect("conexão caiu")
        return await original(db, seed, now)

    monkeypatch.setattr(points_repo, "upsert_by_slug", flaky_upsert)
    path = write_seed(tmp_path, [point("ponto-a"), point("ponto-b"), point("ponto-c")])

    code = await apply(path)

    assert code == 1
    assert await count(app) == 1  # parte já foi gravada
    output = capsys.readouterr().out
    assert "Falha ao acessar o MongoDB (AutoReconnect)" in output
    assert "pode ter sido parcial" in output
    assert "Rode o mesmo comando de novo" in output

    # Rodar de novo completa a importação (é idempotente).
    monkeypatch.setattr(points_repo, "upsert_by_slug", original)
    assert await apply(path) == 0
    assert await count(app, {"active": True}) == 3


# --- erros de validação -----------------------------------------------------


async def test_item_fora_da_area_e_relatado_e_impede_a_gravacao(
    app: FastAPI, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    far = point("ponto-longe", location={"lat": -23.5505, "lng": -46.6333})  # São Paulo
    path = write_seed(tmp_path, [point("ponto-a"), far])

    code = await apply(path)

    assert code == 1
    assert await count(app) == 0
    output = capsys.readouterr().out
    assert "[1] ponto-longe" in output
    assert "fora da área atendida" in output
    assert "Nada foi gravado." in output


async def test_tipo_de_oleo_invalido_e_relatado(
    app: FastAPI, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    path = write_seed(tmp_path, [point("ponto-a", accepted_oils=["oleo_de_cozinha"])])

    code = await apply(path)

    assert code == 1
    assert await count(app) == 0
    output = capsys.readouterr().out
    assert "[0] ponto-a" in output
    assert "accepted_oils" in output


async def test_slug_duplicado_e_relatado(
    app: FastAPI, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    path = write_seed(tmp_path, [point("ponto-a"), point("ponto-a", name="EXEMPLO - outro")])

    code = await apply(path)

    assert code == 1
    assert await count(app) == 0
    output = capsys.readouterr().out
    assert "[1] ponto-a" in output
    assert "slug duplicado" in output


async def test_erros_de_varios_itens_sao_listados_e_nada_e_gravado(
    app: FastAPI, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    items = [
        point("ponto-a"),
        point("ponto-b", accepted_oils=[]),
        point("ponto-c", location={"lat": 0, "lng": 0}),
        point("ponto-d", extra_field="x"),
        point("Slug Inválido"),
        "não é um objeto",
    ]

    code = await apply(write_seed(tmp_path, items))

    assert code == 1
    assert await count(app) == 0
    output = capsys.readouterr().out
    for expected in ("[1] ponto-b", "[2] ponto-c", "[3] ponto-d", "[4] Slug Inválido", "[5]"):
        assert expected in output


async def test_arquivo_vazio_ou_que_nao_e_lista_falha(
    app: FastAPI, tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    await apply(write_seed(tmp_path, [point("ponto-a")]))

    assert await apply(write_seed(tmp_path, [])) == 1  # não pode desativar tudo
    assert await count(app, {"active": True}) == 1

    not_a_list = tmp_path / "objeto.json"
    not_a_list.write_text('{"slug": "x"}', encoding="utf-8")
    assert await apply(not_a_list) == 1
    assert "deve conter uma lista" in capsys.readouterr().out

    assert await run(tmp_path / "nao-existe.json", apply=False) == 1


# --- segurança da saída -----------------------------------------------------


async def test_a_saida_nao_contem_a_senha_da_url(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
):
    url = "mongodb+srv://oleoapp_maint:segredo123@cluster0.abcde.mongodb.net/?x=1"
    monkeypatch.setattr(settings, "MONGO_URL", url)
    path = write_seed(tmp_path, [point("ponto-a", accepted_oils=[])])  # erro: não conecta

    assert await run(path, apply=True) == 1  # sem confirmar o host
    assert await run(path, apply=True, confirm_host="cluster0.abcde.mongodb.net") == 1

    output = capsys.readouterr().out
    assert "cluster0.abcde.mongodb.net" in output
    assert "segredo123" not in output
    assert "oleoapp_maint" not in output


def test_safe_target_remove_usuario_e_senha():
    url = "mongodb://user:p%40ss@localhost:27017/db?authSource=db"
    assert safe_target(url) == "localhost:27017"
    assert safe_target("mongodb+srv://u:senha@cluster0.x.mongodb.net/?retryWrites=true") == (
        "cluster0.x.mongodb.net"
    )
    assert safe_target("mongodb://h1:27017,h2:27017/db") == "h1:27017,h2:27017"
    assert safe_target("mongodb://u:p@a@host:27017") == "host:27017"


def test_validate_items_nao_altera_a_entrada():
    items = [point("ponto-a")]
    snapshot = deepcopy(items)
    seeds, errors = validate_items(items)
    assert len(seeds) == 1
    assert errors == []
    assert items == snapshot

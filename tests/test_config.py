import pytest
from pydantic import ValidationError

from app.core.config import Settings

MONGO_URL = "mongodb://localhost:27017"

PRODUCTION_OK = {
    "ENV": "production",
    "EMAIL_PROVIDER": "brevo",
    "COOKIE_SECURE": True,
    "FRONTEND_URL": "https://oleoapp.exemplo.com",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Só valem os valores passados no teste e os padrões do Settings.
    for name in (
        "EMAIL_PROVIDER",
        "COOKIE_SECURE",
        "FRONTEND_URL",
        "RATE_LIMIT_PASSWORD_FORGOT_PER_EMAIL_PER_HOUR",
    ):
        monkeypatch.delenv(name, raising=False)


def make_settings(**values) -> Settings:
    # _env_file=None: o teste não pode depender de um .env local.
    return Settings(_env_file=None, MONGO_URL=MONGO_URL, **values)


def test_producao_com_configuracao_correta_sobe():
    settings = make_settings(**PRODUCTION_OK)

    assert settings.ENV == "production"


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"EMAIL_PROVIDER": "console"}, "EMAIL_PROVIDER deve ser brevo"),
        ({"COOKIE_SECURE": False}, "COOKIE_SECURE deve ser true"),
        ({"FRONTEND_URL": "http://oleoapp.exemplo.com"}, "FRONTEND_URL deve começar com https://"),
    ],
)
def test_producao_recusa_configuracao_insegura(override: dict, expected: str):
    with pytest.raises(ValidationError, match="Configuração inválida para produção") as info:
        make_settings(**{**PRODUCTION_OK, **override})

    assert expected in str(info.value)


def test_producao_lista_todos_os_problemas_de_uma_vez():
    with pytest.raises(ValueError) as info:
        make_settings(
            ENV="production",
            EMAIL_PROVIDER="console",
            COOKIE_SECURE=False,
            FRONTEND_URL="http://localhost:3000",
        )

    message = str(info.value)
    assert "EMAIL_PROVIDER deve ser brevo" in message
    assert "COOKIE_SECURE deve ser true" in message
    assert "FRONTEND_URL deve começar com https://" in message


def test_producao_sem_variaveis_usa_os_padroes_e_nao_sobe():
    # Os padrões (console e http://localhost:3000) são para desenvolvimento.
    with pytest.raises(ValidationError):
        make_settings(ENV="production")


@pytest.mark.parametrize("env", ["development", "test"])
def test_fora_de_producao_aceita_console_e_http(env: str):
    settings = make_settings(
        ENV=env,
        EMAIL_PROVIDER="console",
        COOKIE_SECURE=False,
        FRONTEND_URL="http://localhost:3000",
    )

    assert settings.EMAIL_PROVIDER == "console"


def test_limite_por_email_do_forgot_tem_padrao_3():
    assert make_settings().RATE_LIMIT_PASSWORD_FORGOT_PER_EMAIL_PER_HOUR == 3

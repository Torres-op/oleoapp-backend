import os
from typing import Literal, Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Versão da API mostrada no /docs e no status do painel de administração.
APP_VERSION = "1.0.0"


def _env_file() -> str | None:
    """Arquivo .env lido pelo Settings. Nos testes (ENV=test no ambiente) ele é ignorado,
    para que a suíte nunca herde o banco, o Redis ou as chaves do desenvolvimento."""
    return None if os.environ.get("ENV") == "test" else ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=_env_file(), env_file_encoding="utf-8", extra="ignore"
    )

    ENV: Literal["development", "test", "production"] = "development"

    # Banco (MongoDB local ou Atlas)
    MONGO_URL: str
    MONGO_DB_NAME: str = "oleoapp"
    MONGO_MAX_POOL_SIZE: int = 10
    MONGO_TIMEOUT_MS: int = 10000

    # Redis (opcional): vazio significa sem rate limit e sem cache
    REDIS_URL: str = ""

    # Sessão
    SESSION_COOKIE_NAME: str = "oleo_session"
    SESSION_TTL_DAYS: int = 7
    COOKIE_SECURE: bool = True
    FRONTEND_URL: str = "http://localhost:3000"

    # Login social (OAuth). Cada provedor só fica ativo com o par id/secret preenchido.
    # URI de retorno: {FRONTEND_URL}/api/auth/oauth/<provedor>/callback (passa pelo rewrite
    # do frontend, então o cookie de sessão fica no domínio do frontend).
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""

    # Google e geocodificação
    GOOGLE_MAPS_API_KEY: str = ""
    GEOCODER: Literal["google", "nominatim"] = "google"
    NOMINATIM_USER_AGENT: str = "OleoApp/1.0"
    # Sugestões de endereço enquanto o usuário digita (Photon, dados do OpenStreetMap).
    # Vale para qualquer GEOCODER: a política do Nominatim público proíbe autocomplete.
    PHOTON_URL: str = "https://photon.komoot.io/api/"

    # Área atendida (Maricá-RJ)
    AREA_CENTER_LAT: float = -22.9189
    AREA_CENTER_LNG: float = -42.8186
    AREA_RADIUS_KM: float = 40.0

    # E-mail
    EMAIL_PROVIDER: Literal["brevo", "console"] = "console"
    BREVO_API_KEY: str = ""
    EMAIL_FROM: str = ""
    EMAIL_FROM_NAME: str = "ÓleoApp"
    PASSWORD_RESET_TTL_MIN: int = 30

    # Rate limit
    RATE_LIMIT_GLOBAL_PER_MIN: int = 120
    RATE_LIMIT_LOGIN_PER_MIN: int = 5
    RATE_LIMIT_SIGNUP_PER_HOUR: int = 5
    RATE_LIMIT_PASSWORD_FORGOT_PER_HOUR: int = 5
    RATE_LIMIT_PASSWORD_FORGOT_PER_EMAIL_PER_HOUR: int = 3
    RATE_LIMIT_DIRECTIONS_PER_HOUR: int = 30
    RATE_LIMIT_GEOCODE_PER_MIN: int = 30
    RATE_LIMIT_GEOCODE_SUGGEST_PER_MIN: int = 60
    RATE_LIMIT_ADMIN_PER_MIN: int = 60

    # Auditoria: eventos de login, cadastro, exclusão, descarte e administração.
    # O Mongo apaga sozinho (índice TTL) os eventos mais antigos que isso.
    AUDIT_LOG_RETENTION_DAYS: int = Field(default=90, ge=1)
    # Exportação CSV dos logs: no máximo esta quantidade de linhas por arquivo.
    ADMIN_EXPORT_MAX_ROWS: int = Field(default=10_000, ge=1)
    # Alerta no painel: falhas de login do mesmo IP (ou e-mail) dentro da janela.
    LOGIN_FAILURE_ALERT_THRESHOLD: int = Field(default=5, ge=1)
    LOGIN_FAILURE_ALERT_WINDOW_MIN: int = Field(default=15, ge=1)

    # Quantos proxies confiáveis acrescentam IPs ao X-Forwarded-For (Render = 1).
    # 0 = sem proxy: usa o IP da conexão.
    TRUSTED_PROXY_HOPS: int = 1

    @model_validator(mode="after")
    def _check_production(self) -> Self:
        """Em produção, recusa subir com configurações que vazam o link de redefinição
        de senha no log ou mandam o cookie de sessão sem HTTPS."""
        if self.ENV != "production":
            return self
        problems: list[str] = []
        if self.EMAIL_PROVIDER != "brevo":
            problems.append("EMAIL_PROVIDER deve ser brevo")
        if not self.COOKIE_SECURE:
            problems.append("COOKIE_SECURE deve ser true")
        if not self.FRONTEND_URL.startswith("https://"):
            problems.append("FRONTEND_URL deve começar com https://")
        if problems:
            raise ValueError(
                "Configuração inválida para produção (ENV=production): " + "; ".join(problems) + "."
            )
        return self


settings = Settings()

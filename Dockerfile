# Mesma imagem para desenvolvimento local e para o Render.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

ARG INSTALL_DEV=false

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && if [ "$INSTALL_DEV" = "true" ]; then pip install --no-cache-dir -r requirements-dev.txt; fi

# Usuário não-root. O /app pertence a ele só para o pytest poder criar o .pytest_cache em dev.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin appuser \
    && chown appuser:appuser /app

# Só o necessário. Nenhum .env entra na imagem.
COPY --chown=appuser:appuser app/ ./app/
COPY --chown=appuser:appuser scripts/ ./scripts/
COPY --chown=appuser:appuser seed/ ./seed/
COPY --chown=appuser:appuser tests/ ./tests/
COPY --chown=appuser:appuser pyproject.toml ./

USER appuser

EXPOSE 8000

# Opcional: o Render usa o próprio health check (/api/health), configurado no painel.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/api/health' % (os.getenv('PORT') or '8000'), timeout=3)"]

# O Render define PORT; localmente o padrão é 8000.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --no-proxy-headers --no-access-log"]

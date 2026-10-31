# ÓleoApp: backend

API do **ÓleoApp**, aplicativo web para encontrar pontos de coleta e descartar óleo lubrificante usado em Maricá-RJ. Este repositório é o único backend; a interface fica no repositório do frontend (Next.js, na Vercel).

## Stack

- Python 3.12, FastAPI, Pydantic v2 e pydantic-settings
- MongoDB com o PyMongo assíncrono (`pymongo.AsyncMongoClient`; **não** usamos Motor)
- Redis (opcional): rate limit e cache. Sem `REDIS_URL`, a API funciona sem eles
- Senhas com Argon2id (`argon2-cffi`), sessão no servidor com cookie HttpOnly
- `httpx` para chamadas externas; testes com pytest, pytest-asyncio e `httpx.ASGITransport`; lint com ruff

## Estrutura de pastas

```
app/
  api/            rotas HTTP (routes/), dependências e router
  core/           configuração, erros, middlewares, rate limit, cache, geo
  db/             nomes das coleções, cliente Mongo e índices
  schemas/        modelos Pydantic (um arquivo por domínio)
  repositories/   acesso ao MongoDB
  services/       regras de negócio
scripts/          scripts de linha de comando (importação de pontos, candidatos)
seed/             dados versionados (pontos de coleta)
tests/            testes automatizados
docker/           arquivos de apoio do compose (init do Mongo)
```

Camadas: `routes` (HTTP) → `services` (regras) → `repositories` (MongoDB).

## Como rodar localmente

Requisitos: Docker com Docker Compose.

```bash
cp .env.example .env
# edite o .env e preencha as senhas (MONGO_ROOT_PASSWORD, MONGO_APP_PASSWORD, REDIS_PASSWORD).
# Gere cada uma com: openssl rand -hex 24
docker compose up --build
```

Depois de subir:

- API: <http://localhost:8000/api/health> (health check, sem banco)
- Estado do Mongo e do Redis: <http://localhost:8000/api/health/ready>
- Documentação interativa: <http://localhost:8000/docs> (desligada em produção)

O Mongo (porta 27017) e a API (porta 8000) ficam acessíveis só em `127.0.0.1`. O Redis não é publicado.

> O usuário do Mongo é criado só na **primeira** inicialização do volume. Se mudar as senhas depois, apague o volume (`docker compose down -v`, o que apaga os dados locais).

## Testes e lint

```bash
docker compose run --rm api pytest
docker compose run --rm api ruff check .
docker compose run --rm api ruff format --check .
```

Fora do Docker (com Mongo e Redis locais disponíveis):

```bash
pip install -r requirements.txt -r requirements-dev.txt
MONGO_URL=mongodb://localhost:27017 REDIS_URL=redis://localhost:6379/15 pytest
```

Os testes usam o banco `oleoapp_test` e limpam o banco e o Redis antes de cada teste.

## Importar pontos de coleta

O formato do arquivo está em `seed/collection_points.example.json`. O script roda por padrão em **modo de simulação** e só grava com `--apply`.

Local (dentro do compose):

```bash
docker compose exec api python -m scripts.import_points seed/collection_points.json
docker compose exec api python -m scripts.import_points seed/collection_points.json --apply
```

Se a gravação for desativar pontos que não estão no arquivo (por exemplo, um arquivo parcial), o `--apply` só prossegue com `--allow-deactivate`, depois de você conferir na simulação a lista "será desativado".

Pontos criados ou editados no painel de admin (`managed_by: "admin"`) nunca são alterados nem desativados pelo import, nem com `--allow-deactivate`; a simulação lista esses pontos em "Mantidos pelo admin".

No Atlas (destino remoto, que também exige `--confirm-host`): veja `docs/deploy.md`.

De onde vêm os pontos:

- **OpenStreetMap (fonte principal):** `python -m scripts.osm_points` busca oficinas, postos, borracharias, autopeças e oficinas de moto de Maricá na Overpass API, completa rua, bairro e CEP com a busca reversa do Nominatim e grava `seed/collection_points.json`. Os pontos entram como não verificados (`verified: false`), com o aviso "ligue antes". Licença ODbL: o app mostra "© colaboradores do OpenStreetMap" na lista de pontos.
- **Curadoria (opcional):** `python -m scripts.places_candidates` lista candidatos do Google num CSV; a equipe confirma cada um numa planilha; `python -m scripts.seed_from_curation curadoria.csv --merge` acrescenta as linhas verificadas ao seed. Do Google só vem o `google_place_id`, porque os termos do Google não permitem guardar nome, endereço ou coordenadas.

## Variáveis de ambiente

| Variável | Padrão | Finalidade |
|---|---|---|
| `ENV` | `development` | `development`, `test` ou `production` (produção desliga `/docs`) |
| `MONGO_URL` | (obrigatória) | Conexão com o MongoDB (no compose local, montada a partir das senhas) |
| `MONGO_DB_NAME` | `oleoapp` | Nome do banco |
| `MONGO_MAX_POOL_SIZE` | `10` | Tamanho máximo do pool de conexões |
| `MONGO_TIMEOUT_MS` | `10000` | Tempo para escolher servidor e conectar |
| `REDIS_URL` | vazio | Rate limit e cache; vazio desliga os dois |
| `SESSION_COOKIE_NAME` | `oleo_session` | Nome do cookie de sessão |
| `SESSION_TTL_DAYS` | `7` | Validade da sessão |
| `COOKIE_SECURE` | `true` | Cookie só via HTTPS (use `false` no desenvolvimento local) |
| `FRONTEND_URL` | `http://localhost:3000` | Base dos links de redefinição de senha e das URIs de retorno do login social |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | vazio | Login com Google; vazio desliga o botão (veja `docs/login-social.md`) |
| `GOOGLE_MAPS_API_KEY` | vazio | Routes e Geocoding no servidor; Places só no script de candidatos |
| `GEOCODER` | `google` | `google` ou `nominatim` |
| `NOMINATIM_USER_AGENT` | `OleoApp/1.0` | Exigido pela política do Nominatim (também enviado ao Photon) |
| `PHOTON_URL` | `https://photon.komoot.io/api/` | Sugestões de endereço enquanto o usuário digita (`/api/geocode/suggest`), com dados do OpenStreetMap |
| `AREA_CENTER_LAT` / `AREA_CENTER_LNG` / `AREA_RADIUS_KM` | `-22.9189` / `-42.8186` / `40` | Área atendida (Maricá) |
| `EMAIL_PROVIDER` | `console` | `brevo` ou `console` (o console só vale fora de produção) |
| `BREVO_API_KEY`, `EMAIL_FROM`, `EMAIL_FROM_NAME` | vazio, vazio, `ÓleoApp` | Envio de e-mail |
| `PASSWORD_RESET_TTL_MIN` | `30` | Validade do link de redefinição de senha |
| `RATE_LIMIT_GLOBAL_PER_MIN` | `120` | Limite global por IP |
| `RATE_LIMIT_LOGIN_PER_MIN` | `5` | Login, por IP + e-mail |
| `RATE_LIMIT_SIGNUP_PER_HOUR` | `5` | Cadastro, por IP |
| `RATE_LIMIT_PASSWORD_FORGOT_PER_HOUR` | `5` | "Esqueci a senha", por IP |
| `RATE_LIMIT_DIRECTIONS_PER_HOUR` | `30` | Rotas, por usuário |
| `RATE_LIMIT_GEOCODE_PER_MIN` | `30` | Busca de endereço, por usuário |
| `RATE_LIMIT_GEOCODE_SUGGEST_PER_MIN` | `60` | Sugestões de endereço, por usuário |
| `RATE_LIMIT_ADMIN_PER_MIN` | `60` | Rotas `/api/admin/*`, por administrador |
| `AUDIT_LOG_RETENTION_DAYS` | `90` | Dias que os eventos de auditoria ficam guardados (índice TTL) |
| `ADMIN_EXPORT_MAX_ROWS` | `10000` | Máximo de linhas na exportação CSV dos logs |
| `LOGIN_FAILURE_ALERT_THRESHOLD` | `5` | Falhas de login (mesmo IP ou e-mail) que disparam o alerta do painel |
| `LOGIN_FAILURE_ALERT_WINDOW_MIN` | `15` | Janela, em minutos, contada pelo alerta de falhas de login |

Só no desenvolvimento local (lidas pelo `docker compose`): `MONGO_ROOT_USER`, `MONGO_ROOT_PASSWORD`, `MONGO_APP_USER`, `MONGO_APP_PASSWORD` e `REDIS_PASSWORD`. A porta é lida de `PORT` (definida pelo Render; padrão local 8000).

## Administração

O painel de administração (`/admin` no site) usa as rotas `/api/admin/*`, que só respondem para contas com `role: "admin"` (401 sem login, 403 sem permissão). Não existe rota que promova alguém a admin: o primeiro administrador é criado por script, com acesso ao banco.

```bash
docker compose exec api python -m scripts.create_admin --email voce@exemplo.com
docker compose exec api python -m scripts.create_admin --email voce@exemplo.com --revoke  # tira o papel
```

Se a conta não existe, o script pede a senha duas vezes (sem mostrar na tela) e cria a conta já como admin; se existe, só promove. Para rodar sem perguntas, use as variáveis opcionais `ADMIN_EMAIL` e `ADMIN_PASSWORD` só nesse comando. Não há senha padrão.

A coleção `audit_logs` registra login (sucesso e falha, com o e-mail mascarado), logout, cadastro, exclusão de conta, descarte e ações de admin, com IP e navegador, sem senhas nem tokens. Ao excluir uma conta, os eventos dela perdem o id e o e-mail.

Ações do admin pelo painel:

- **Desativar/reativar conta** (`POST /api/admin/users/{id}/deactivate` e `/reactivate`): a conta desativada perde as sessões e não entra mais (senha ou Google). Admins e a própria conta não podem ser desativados.
- **Pontos de coleta** (`GET/POST /api/admin/points`, `GET/PATCH /api/admin/points/{id}`): criar, editar e desativar pontos, com as mesmas validações do seed. Esses pontos ficam marcados como mantidos pelo admin.
- **Exportar logs** (`GET /api/admin/logs/export`): CSV com os mesmos filtros da tela de logs, até `ADMIN_EXPORT_MAX_ROWS` linhas.
- **Alertas** (`GET /api/admin/alerts`): IPs ou e-mails com `LOGIN_FAILURE_ALERT_THRESHOLD` falhas de login ou mais nos últimos `LOGIN_FAILURE_ALERT_WINDOW_MIN` minutos.

## Convenções

- Todas as rotas ficam sob `/api`; JSON em `snake_case`; datas em ISO 8601 UTC.
- Erros sempre no formato `{"error": {"code": "...", "message": "...", "fields": [...] | null}}`, levantados com `AppError`.
- `POST`, `PUT`, `PATCH` e `DELETE` exigem o cabeçalho `X-OleoApp-Client: web`.
- A API não grava nada em disco; logs só na saída padrão.
- Branch: `tarefa/<ID>-<resumo>` (ex.: `tarefa/BE-03-autenticacao`). Uma tarefa = um PR.
- Commits no padrão convencional, em português: `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`.
- Antes do PR: `ruff check .`, `ruff format --check .` e `pytest` passando.

## Produção

Produção: Render (Docker, plano gratuito) + MongoDB Atlas M0, ver `docs/deploy.md`.

## Riscos aceitos

- A chave do Google é uma chave "demo" sem restrições configuráveis nem acompanhamento de cotas. Mitigação: o navegador usa a chave só para desenhar o mapa; Routes e Geocoding passam pelo backend, com login e rate limit.
- As sugestões de endereço usam a instância pública do Photon (komoot), que não tem SLA e pede uso moderado. Mitigação: só com login, depois de uma pausa na digitação, com cache de 24 h no Redis e limite por usuário. Se o uso crescer, aponte `PHOTON_URL` para uma instância própria. O Nominatim público não serve para isso, porque a política dele proíbe autocomplete.
- O plano gratuito do Render dorme após 15 minutos sem tráfego; a primeira requisição depois disso leva cerca de 1 minuto. O frontend trata esse atraso.

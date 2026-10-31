# Deploy: Render + MongoDB Atlas M0

Este documento descreve como colocar a API do ÓleoApp em produção e como operá-la depois. A configuração fica **nos painéis** do Render e do Atlas; não há `render.yaml` nem compose de produção no repositório.

Convenções:

- **[PAINEL]** marca ações manuais em um painel (Atlas, Render, GitHub, Brevo).
- Cada passo termina com **Verificação**: só siga adiante quando ela passar.
- Valores entre `< >` são marcadores (ex.: `<servico>`, `<cluster>`, `<regiao>`). **Nunca** coloque senhas, chaves ou URIs reais neste arquivo nem em qualquer arquivo do git; elas ficam no cofre do time e nos painéis.

## Sumário

0. [Visão geral](#0-visão-geral)
1. [Branch de deploy (`main`)](#1-branch-de-deploy-main)
2. [Atlas: projeto e cluster M0](#2-painel-atlas-projeto-e-cluster-m0)
3. [Atlas: usuários do banco](#3-painel-atlas-usuários-do-banco)
4. [Render: Key Value](#4-painel-render-key-value)
5. [Render: Web Service e variáveis](#5-painel-render-web-service-e-variáveis)
6. [Render → Atlas: IPs de saída](#6-painel-render--atlas-ips-de-saída)
7. [Se as faixas de saída mudarem](#7-se-as-faixas-de-saída-do-render-mudarem)
8. [Importação dos pontos de coleta](#8-importação-dos-pontos-de-coleta)
9. [Teste de fumaça](#9-teste-de-fumaça-direto-no-render)
10. [Rotação de senha do `oleoapp_api`](#10-rotação-de-senha-do-oleoapp_api)
11. [Cold start](#11-cold-start)
12. [Backups e restauração](#12-backups-e-restauração)
13. [Operação, alertas e limites](#13-operação-alertas-e-limites)
14. [Compensações se for preciso liberar `0.0.0.0/0`](#14-compensações-se-for-preciso-liberar-00000)
15. [Registro do deploy](#15-registro-do-deploy)

---

## 0. Visão geral

```
Navegador ──► Vercel (frontend Next.js)
                 │  rewrite /api/*  ──►  Render Web Service (API, Docker, Free)
                 │                           │            │
                 │                           ▼            ▼
                 │                Render Key Value   MongoDB Atlas M0
                 │                (Free, só memória)  (lista de acesso restrita)
```

| Peça | Plano | O que importa saber |
|---|---|---|
| **Vercel** (frontend) | Gratuito | O navegador chama sempre `https://<projeto>.vercel.app/api/...`; a Vercel repassa (rewrite) para o Render. Assim o cookie de sessão é de primeira parte e não há CORS. A Vercel espera até 120 s pela resposta do Render. |
| **Render Web Service** (API) | Free, runtime Docker, 1 instância | Dorme após **15 min** sem tráfego e leva **~1 min** para acordar. Disco **efêmero** (a API não grava nada em disco). **Sem shell** e sem tarefas avulsas. Pode reiniciar a qualquer momento. Escuta na porta definida em `PORT`. Portas SMTP bloqueadas (e-mail só por API HTTP). |
| **Render Key Value** | Free | Guarda dados **só em memória** (rate limit e cache). Perder os dados num reinício não causa problema. Um por workspace. |
| **MongoDB Atlas** | M0 (gratuito) | **0,5 GB** de armazenamento; até **500 conexões**; **sem backup automático**; o cluster é **pausado após 30 dias sem conexões**. MongoDB 8.0 (mesma versão da imagem local `mongo:8`). |

---

## 1. Branch de deploy (`main`)

O Render faz deploy a partir da branch **`main`** do GitHub. O desenvolvimento acontece na `dev`.

1. **[PAINEL GitHub]** Antes do primeiro deploy, abra um **Pull Request `dev` → `main` no GitHub** e faça o merge pela interface do GitHub (com o CI verde).
2. **Nunca** envie (`git push`) a `main` **local antiga** de nenhuma máquina. Ela contém histórico anterior à limpeza, com uma credencial que foi removida do repositório. Se precisar da `main` localmente, recrie-a a partir do remoto depois do merge:

   ```bash
   git fetch origin
   git checkout -B main origin/main
   ```

3. A partir daí, todo deploy nasce de um PR mesclado na `main` (o auto-deploy do passo 5 cuida do resto).

**Verificação:** no GitHub, a `main` mostra o merge do PR `dev` → `main` como último commit, e o CI (`Backend CI`) está verde nele.

---

## 2. [PAINEL Atlas] Projeto e cluster M0

1. **[PAINEL Atlas]** Crie um **projeto novo** (ex.: `oleoapp-prod`). **Não** reutilize o projeto do MVP: a lista de acesso dele provavelmente está liberada para `0.0.0.0/0`.
2. **[PAINEL Atlas]** Crie um cluster **M0** (Free) no provedor e na região **mais próximos da região escolhida no Render** (decisão DA2). Anote os dois na [seção 15](#15-registro-do-deploy).
3. Os dados ficarão fora do Brasil; isso deve constar na política de privacidade do frontend.

**Verificação:** o cluster `<cluster>` aparece como ativo no painel do projeto novo.

---

## 3. [PAINEL Atlas] Usuários do banco

Caminho: **Database Access** (nas versões novas da interface, **Database & Network Access**) → **Database Users** → **Add New Database User** → método **Password** → em **Database User Privileges**, escolha **Specific Privileges**.

| Usuário | Privilégio | Uso |
|---|---|---|
| `oleoapp_api` | `readWrite` no banco `oleoapp` | Só a API no Render. Em **Restrict Access to Specific Clusters**, marque **só** o cluster `<cluster>`. |
| `oleoapp_maint` | `readWrite` no banco `oleoapp` | Só importação de pontos, por 1 ou 2 pessoas. |
| `oleoapp_backup` | `read` no banco `oleoapp` | Só `mongodump`. |

1. **[PAINEL Atlas]** Crie os três usuários conforme a tabela.
2. Gere cada senha com:

   ```bash
   openssl rand -hex 24
   ```

   (hexadecimal: 48 caracteres, sem caracteres especiais que precisariam ser escapados na URI). Guarde as senhas **só no cofre do time**.
3. **Proibido:** `readWriteAnyDatabase`, `atlasAdmin` ou qualquer papel amplo. Nenhum outro usuário amplo deve existir no projeto.
4. Para uma manutenção pontual, o Atlas também permite **usuários temporários** (expiram em até 7 dias).

**Verificação:** em Database Users, os três usuários aparecem com os papéis acima, todos restritos ao banco `oleoapp`, e o `oleoapp_api` restrito ao cluster `<cluster>`.

---

## 4. [PAINEL Render] Key Value

1. **[PAINEL Render]** **New** → **Key Value** → instância **Free**, na **mesma região** que será usada pela API (`<regiao>`).
2. **[PAINEL Render]** Após criar, copie a **Internal URL** (a URL interna, não a externa). Ela será o `REDIS_URL` da API.

**Verificação:** a instância aparece como disponível e a Internal URL foi copiada para o cofre do time (ou direto para o passo 5).

---

## 5. [PAINEL Render] Web Service e variáveis

### 5.1 Criar o serviço

1. **[PAINEL Render]** **New** → **Web Service** → conecte o repositório **`OleoAppBackEnd`**.
2. Configure:

   | Campo | Valor |
   |---|---|
   | Branch | `main` |
   | Runtime / Language | **Docker** |
   | Dockerfile Path | `./Dockerfile` (raiz do repositório) |
   | Docker Command | vazio (o `CMD` do Dockerfile já roda o uvicorn em `$PORT`) |
   | Instance Type | **Free** |
   | Region | `<regiao>` (a mesma do Key Value) |
   | Health Check Path | `/api/health` |
   | Auto-Deploy | ligado (deploy a cada commit na `main`) |

3. Não defina o build arg `INSTALL_DEV` (as dependências de desenvolvimento não vão para produção).

### 5.2 Variáveis de ambiente

**[PAINEL Render]** Em **Environment**, cadastre as variáveis abaixo. A tabela tem só nomes e exemplos **sem segredos**; os valores reais vêm do cofre do time.

| Variável | Valor em produção (exemplo) | Finalidade |
|---|---|---|
| `PORT` | **não cadastre** | Definida pelo próprio Render (padrão 10000). O Dockerfile lê essa variável. |
| `ENV` | `production` | Desliga `/docs`, `/redoc` e `/openapi.json` e ativa as checagens de produção (abaixo). |
| `MONGO_URL` | `mongodb+srv://oleoapp_api:<senha>@<cluster>.mongodb.net/?retryWrites=true&w=majority&appName=oleoapp-api` | Conexão com o Atlas (TLS), com o usuário `oleoapp_api`. Copie o host exato do botão **Connect** do cluster. |
| `MONGO_DB_NAME` | `oleoapp` | Nome do banco. |
| `MONGO_MAX_POOL_SIZE` | `10` | Tamanho máximo do pool (o M0 aceita até 500 conexões). |
| `MONGO_TIMEOUT_MS` | `10000` | Tempo para escolher servidor e conectar. |
| `REDIS_URL` | Internal URL do Key Value (passo 4) | Rate limit e cache. Vazio desliga os dois (não deixe vazio em produção). |
| `SESSION_COOKIE_NAME` | `oleo_session` | Nome do cookie de sessão. |
| `SESSION_TTL_DAYS` | `7` | Validade da sessão. |
| `COOKIE_SECURE` | `true` | Cookie só via HTTPS. |
| `FRONTEND_URL` | `https://<projeto>.vercel.app` | Base dos links de redefinição de senha e das URIs de retorno do login social. Deve começar com `https://`. |
| `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` | do Google Cloud (cofre) | Opcional. Login com Google; veja `docs/login-social.md`. |
| `GOOGLE_MAPS_API_KEY` | a chave (cofre) | Routes e Geocoding no backend. |
| `GEOCODER` | `google` ou `nominatim` | Conforme o resultado da tarefa G-03. |
| `NOMINATIM_USER_AGENT` | `OleoApp/1.0 (contato: <e-mail-de-contato>)` | Exigido pela política do Nominatim (também enviado ao Photon). |
| `PHOTON_URL` | `https://photon.komoot.io/api/` | Opcional. Sugestões de endereço enquanto o usuário digita. |
| `AREA_CENTER_LAT` / `AREA_CENTER_LNG` / `AREA_RADIUS_KM` | `-22.9189` / `-42.8186` / `40` | Área atendida (Maricá-RJ). |
| `EMAIL_PROVIDER` | `brevo` | Em produção só `brevo` é aceito. |
| `BREVO_API_KEY` | a chave do Brevo (cofre) | Envio de e-mail pela API HTTP (o Render gratuito bloqueia SMTP). |
| `EMAIL_FROM` / `EMAIL_FROM_NAME` | remetente verificado no Brevo / `ÓleoApp` | Remetente dos e-mails. |
| `PASSWORD_RESET_TTL_MIN` | `30` | Validade do link de redefinição. |
| `RATE_LIMIT_GLOBAL_PER_MIN` | `120` | Limite global por IP. |
| `RATE_LIMIT_LOGIN_PER_MIN` | `5` | Login, por IP + e-mail. |
| `RATE_LIMIT_SIGNUP_PER_HOUR` | `5` | Cadastro, por IP. |
| `RATE_LIMIT_PASSWORD_FORGOT_PER_HOUR` | `5` | "Esqueci a senha", por IP. |
| `RATE_LIMIT_PASSWORD_FORGOT_PER_EMAIL_PER_HOUR` | `3` | "Esqueci a senha", por e-mail informado. |
| `RATE_LIMIT_DIRECTIONS_PER_HOUR` | `30` | Rotas, por usuário. |
| `RATE_LIMIT_GEOCODE_PER_MIN` | `30` | Busca de endereço, por usuário. |
| `RATE_LIMIT_GEOCODE_SUGGEST_PER_MIN` | `60` | Opcional. Sugestões de endereço, por usuário. |
| `RATE_LIMIT_ADMIN_PER_MIN` | `60` | Opcional. Rotas de administração, por admin. |
| `AUDIT_LOG_RETENTION_DAYS` | `90` | Opcional. Retenção dos eventos de auditoria (dias). |
| `ADMIN_EXPORT_MAX_ROWS` | `10000` | Opcional. Máximo de linhas na exportação CSV dos logs. |
| `LOGIN_FAILURE_ALERT_THRESHOLD` | `5` | Opcional. Falhas de login (mesmo IP ou e-mail) que disparam o alerta do painel. |
| `LOGIN_FAILURE_ALERT_WINDOW_MIN` | `15` | Opcional. Janela, em minutos, do alerta de falhas de login. |
| `TRUSTED_PROXY_HOPS` | `1` | Quantos proxies confiáveis acrescentam IPs ao `X-Forwarded-For` (Render = 1; sem proxy = 0). Ver nota abaixo. |

Os valores de `RATE_LIMIT_*`, `TRUSTED_PROXY_HOPS`, `MONGO_MAX_POOL_SIZE`, `MONGO_TIMEOUT_MS`, `SESSION_*` e `AREA_*` já têm esses padrões no código; cadastrá-los no painel é opcional, mas deixa a configuração explícita.

Não são necessárias: segredo de sessão ou de JWT (as sessões usam tokens aleatórios guardados como hash) nem origens de CORS (não há CORS com o rewrite da Vercel).

**Checagens de produção.** Com `ENV=production`, a API **recusa subir** se:

- `EMAIL_PROVIDER` não for `brevo`;
- `COOKIE_SECURE` não for `true`;
- `FRONTEND_URL` não começar com `https://`.

Nesses casos o deploy falha (o health check nunca passa) e o motivo aparece em **Logs**. Corrija a variável no painel; o Render faz um novo deploy.

**Nota sobre o IP do cliente (`TRUSTED_PROXY_HOPS`).** O uvicorn roda com `--no-proxy-headers`; quem decide o IP do cliente é a própria API, que usa a entrada do `X-Forwarded-For` na posição indicada por `TRUSTED_PROXY_HOPS`, contando da direita (com `1`, a entrada mais à direita, que é a acrescentada pelo proxy do Render). Esse IP alimenta o rate limit por IP.

- **Validar no primeiro deploy do frontend (FE-10):** fazer requisições pela Vercel e conferir qual IP a API enxerga (por exemplo, provocando o rate limit de login a partir de duas redes diferentes). Se todas as pessoas aparecerem com o **IP da Vercel**, o rate limit por IP passa a ser compartilhado por todos os usuários.
- Nesse caso, avaliar `TRUSTED_PROXY_HOPS=2` (usa a entrada acrescentada pela Vercel). **Atenção:** com `2`, quem chamar `https://<servico>.onrender.com` **diretamente** consegue forjar o próprio IP (basta enviar um `X-Forwarded-For` inventado), contornando o rate limit por IP. Registre a decisão na [seção 15](#15-registro-do-deploy).

### 5.3 Primeiro deploy

1. **[PAINEL Render]** Clique em **Create Web Service** e acompanhe o build em **Logs**.

**Verificação:**

```bash
curl -i https://<servico>.onrender.com/api/health
# esperado: HTTP 200 e {"status":"ok"}

curl -i https://<servico>.onrender.com/api/health/ready
# esperado AGORA: HTTP 503 (o Atlas ainda não libera o Render; isso é normal)
```

---

## 6. [PAINEL Render → Atlas] IPs de saída

1. **[PAINEL Render]** Abra o serviço → **Connect** (canto superior direito) → aba **Outbound** → copie **cada** faixa em notação CIDR. Use só os valores do painel; não use IPs de exemplos.
2. **[PAINEL Atlas]** No projeto → **Security** → **Network Access** / **IP Access List** (nas versões novas, **Database & Network Access**) → **Add IP Address** → cole **uma faixa por entrada**, com o comentário `Render <regiao>`. Repita para todas as faixas.
3. **Não** adicione `0.0.0.0/0`. Se um dia for inevitável, siga a [seção 14](#14-compensações-se-for-preciso-liberar-00000).

Atenção:

- Essas faixas são **compartilhadas por todos os serviços do Render na mesma região**, não são exclusivas do ÓleoApp. A lista de acesso reduz muito a exposição em relação a `0.0.0.0/0`, mas **a senha forte do `oleoapp_api` continua sendo a proteção principal**.
- IPs de saída dedicados existem no Render, mas são pagos.

**Verificação:**

```bash
curl -i https://<servico>.onrender.com/api/health/ready
# esperado: HTTP 200 e {"status":"ok","mongo":"ok","redis":"ok"}
```

E, no Atlas, a IP Access List **não** contém `0.0.0.0/0`, só as faixas do Render (e entradas temporárias perto de vencer).

---

## 7. Se as faixas de saída do Render mudarem

**Sintomas:**

- `/api/health/ready` passa a responder **503**;
- nos **Logs** do Render aparecem `ServerSelectionTimeoutError` (ou "Ping no Mongo falhou").

**O que fazer:**

1. Acompanhe o **changelog do Render**, onde mudanças de faixas são anunciadas.
2. Repita o [passo 6](#6-painel-render--atlas-ips-de-saída) com as faixas novas.
3. **[PAINEL Atlas]** Remova as faixas antigas da IP Access List.

**Verificação:** `/api/health/ready` volta a responder 200 com `"mongo":"ok"`.

---

## 8. Importação dos pontos de coleta

O Render gratuito não tem shell, então a importação roda **da máquina de uma pessoa**, com o usuário `oleoapp_maint`.

Pré-requisitos: Python 3.12 com as dependências do projeto (`pip install -r requirements.txt`, de preferência num ambiente virtual), na raiz do repositório, e o arquivo curado `seed/collection_points.json` (formato em `seed/collection_points.example.json`).

1. **[PAINEL Atlas]** IP Access List → **Add IP Address** → **Add Current IP Address**, marcando a opção de entrada **temporária** (o Atlas apaga sozinho em até 7 dias; escolha o menor prazo). Comentário: `importação <nome> <data>`.
2. Rode a **simulação** (não grava nada). As variáveis passadas na linha de comando têm prioridade sobre um `.env` local:

   ```bash
   MONGO_URL="<URI do oleoapp_maint>" MONGO_DB_NAME=oleoapp \
     python -m scripts.import_points seed/collection_points.json
   ```

   - O script imprime a linha `Destino: <host> (banco: oleoapp)` **antes de qualquer coisa**. Confira se é o cluster de produção e o banco `oleoapp`, e anote o host: ele será usado no passo 3.
   - Confira o resumo: `Novos`, `Atualizados`, `A desativar` e `Erros: 0`.
3. Grave com `--apply`. Quando o destino **não é local** (localhost, 127.0.0.1 ou mongo), o `--apply` exige `--confirm-host <host>`, com o host **exatamente** como apareceu na linha `Destino:` da simulação; sem ele, nada é gravado:

   ```bash
   MONGO_URL="<URI do oleoapp_maint>" MONGO_DB_NAME=oleoapp \
     python -m scripts.import_points seed/collection_points.json --apply --confirm-host <host>
   ```

4. **Desativação de pontos.** Pontos ativos no banco que **não estão** no arquivo só são desativados se você passar também `--allow-deactivate`. Use essa opção apenas quando a simulação listar, em `será desativado: <slug>`, exatamente os pontos que devem sair:

   ```bash
   MONGO_URL="<URI do oleoapp_maint>" MONGO_DB_NAME=oleoapp \
     python -m scripts.import_points seed/collection_points.json --apply --allow-deactivate --confirm-host <host>
   ```

   Pontos criados ou editados no painel de admin ficam de fora: o import não os altera nem os desativa (a simulação lista em "Mantidos pelo admin").

5. **[PAINEL Atlas]** Remova a entrada temporária da IP Access List assim que terminar (não espere o vencimento).

**Verificação:** no Atlas (**Browse Collections** → `oleoapp.collection_points`), a contagem de documentos ativos bate com o arquivo; a entrada temporária foi removida. Depois do login do [passo 9](#9-teste-de-fumaça-direto-no-render), `curl -b cookies.txt "https://<servico>.onrender.com/api/points/near?lat=-22.9189&lng=-42.8186&radius_m=50000"` devolve os pontos (a rota exige sessão).

---

## 9. Teste de fumaça direto no Render

Use um e-mail de teste seu. `POST`, `PUT`, `PATCH` e `DELETE` exigem o cabeçalho `X-OleoApp-Client: web`. A senha precisa ter de 8 a 128 caracteres, com ao menos uma letra maiúscula e um número.

1. Health:

   ```bash
   curl -i https://<servico>.onrender.com/api/health
   ```

   **Verificação:** 200.

2. Cadastro (guarda o cookie em `cookies.txt`, fora do repositório):

   ```bash
   curl -i -c cookies.txt \
     -H "Content-Type: application/json" -H "X-OleoApp-Client: web" \
     -d '{"name":"Teste Deploy","email":"<e-mail-de-teste>","password":"<senha-de-teste>"}' \
     https://<servico>.onrender.com/api/auth/signup
   ```

   **Verificação:** 201 e um cabeçalho `set-cookie: oleo_session=...` com **`HttpOnly`**, **`Secure`** e **`SameSite=lax`**.

3. Login e sessão:

   ```bash
   curl -i -c cookies.txt \
     -H "Content-Type: application/json" -H "X-OleoApp-Client: web" \
     -d '{"email":"<e-mail-de-teste>","password":"<senha-de-teste>"}' \
     https://<servico>.onrender.com/api/auth/login

   curl -i -b cookies.txt https://<servico>.onrender.com/api/auth/me
   ```

   **Verificação:** 200 nos dois; o `/api/auth/me` devolve o usuário de teste.

4. Requisição sem o cabeçalho de cliente:

   ```bash
   curl -i -H "Content-Type: application/json" -d '{}' https://<servico>.onrender.com/api/auth/login
   ```

   **Verificação:** 403 com `"code":"forbidden"` (a API não processa o login).

5. "Esqueci a senha":

   ```bash
   curl -i -H "Content-Type: application/json" -H "X-OleoApp-Client: web" \
     -d '{"email":"<e-mail-de-teste>"}' \
     https://<servico>.onrender.com/api/auth/password/forgot
   ```

   **Verificação:** **204 imediato** (a resposta é sempre 204, exista ou não a conta). O e-mail é enviado **em segundo plano**, depois da resposta; por isso uma falha do Brevo **não** aparece na resposta, só nos **Logs** do Render. Confira se o e-mail chegou à caixa de teste e se o link aponta para `FRONTEND_URL`.

6. Documentação desligada:

   ```bash
   curl -i https://<servico>.onrender.com/docs
   ```

   **Verificação:** 404.

7. Limpeza: apague a conta de teste (`POST /api/auth/delete-account` com o cookie e o cabeçalho `X-OleoApp-Client: web`) e o arquivo `cookies.txt`.

8. Auto-deploy: mescle um PR pequeno na `main`.

   **Verificação:** o Render inicia um deploy sozinho, e `/api/health` continua 200 ao final.

---

## 10. Rotação de senha do `oleoapp_api`

Quando: sempre que alguém sair da equipe, se houver suspeita de vazamento, ou **a cada 6 meses**.

1. Gere a nova senha: `openssl rand -hex 24`. Guarde no cofre do time.
2. **[PAINEL Atlas]** Database Users → editar `oleoapp_api` → **Edit Password** → nova senha → **Update User**.
3. **[PAINEL Render]** Environment → atualize `MONGO_URL` com a nova senha → salve. O Render faz um novo deploy.
4. Espere o deploy terminar.

**Verificação:** `/api/health/ready` responde 200 com `"mongo":"ok"`. Registre a data na [seção 15](#15-registro-do-deploy).

O mesmo procedimento vale para `oleoapp_maint` e `oleoapp_backup` (sem o passo 3).

---

## 11. Cold start

- O serviço gratuito **dorme após 15 min sem tráfego**; a primeira requisição depois disso leva **cerca de 1 min**.
- O frontend trata isso: mostra a faixa "Acordando o servidor…", espera até 90 s por requisição e repete automaticamente só requisições `GET`. A Vercel espera até 120 s pela resposta do Render.
- **Não** configure monitores externos (uptime, cron, ping) que chamem a API periodicamente: isso impede o serviço de dormir e consome as horas gratuitas do workspace.

**Teste de cold start:**

1. Deixe o serviço **20 minutos** sem nenhum tráfego.
2. Meça:

   ```bash
   curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" https://<servico>.onrender.com/api/health
   ```

3. Anote o resultado na tabela abaixo. Se ficar bem acima de ~1 min, avalie trocar o runtime Docker pelo runtime Python nativo do Render (build `pip install -r requirements.txt`; start `uvicorn app.main:app --host 0.0.0.0 --port $PORT --no-proxy-headers`).

| Data | Região | Status | Tempo total | Observação |
|---|---|---|---|---|
| `<AAAA-MM-DD>` | `<regiao>` | `<status>` | `<segundos>` | |

---

## 12. Backups e restauração

O M0 **não tem backup automático**. O backup roda **fora do Render** (máquina de uma pessoa responsável), com as MongoDB Database Tools instaladas, fora dos repositórios.

**O que guardar:** o banco `oleoapp` inteiro. Os pontos também estão no git (`seed/`); o que realmente importa são `users`, `disposals` e `favorites`.

### 12.1 Backup (semanal, com lembrete na agenda)

1. **[PAINEL Atlas]** Adicione uma entrada **temporária** com o seu IP (comentário `backup <nome> <data>`).
2. Rode:

   ```bash
   mongodump --uri "<URI do oleoapp_backup>" --db oleoapp --gzip --archive=oleoapp-AAAAMMDD.archive.gz
   ```

3. Guarde o arquivo numa pasta **restrita** (ex.: o Drive da conta do projeto, com 2FA). Ele contém e-mails e hashes de senha: trate como **dado pessoal**. Nunca coloque o arquivo em um repositório.
4. Retenção: **4 cópias semanais + 3 mensais**; apague as mais antigas.
5. **[PAINEL Atlas]** Remova a entrada temporária.

**Verificação:** o arquivo `oleoapp-AAAAMMDD.archive.gz` existe na pasta restrita e o `mongodump` terminou sem erro.

No M0 não é possível fazer dump do banco `admin` nem usar `--dumpDbUsersAndRoles`: os usuários do Atlas **não** vão no backup e são recriados manualmente pelo [passo 3](#3-painel-atlas-usuários-do-banco).

### 12.2 Teste de restauração (mensal)

1. No Mongo local (o do `docker compose`), restaure num banco separado:

   ```bash
   mongorestore --uri "<URI do Mongo local>" --gzip --archive=oleoapp-AAAAMMDD.archive.gz \
     --nsFrom 'oleoapp.*' --nsTo 'oleoapp_restore.*'
   ```

2. Compare `countDocuments()` por coleção (`users`, `sessions`, `password_resets`, `collection_points`, `disposals`, `favorites`) entre `oleoapp_restore` (local) e `oleoapp` (Atlas, no Browse Collections).
3. Registre a data do teste na [seção 15](#15-registro-do-deploy) e apague o banco `oleoapp_restore` local.

**Verificação:** as contagens batem (diferenças pequenas só nas coleções que mudaram depois do dump).

---

## 13. Operação, alertas e limites

### 13.1 Onde olhar

- **Logs:** Render → serviço → **Logs**. A API registra método, caminho, status e duração, e exceções sem dados pessoais. Falhas de envio de e-mail do "esqueci a senha" (que roda em segundo plano) aparecem só aqui.
- **Estado sob demanda:** `GET https://<servico>.onrender.com/api/health/ready` (`mongo` e `redis`: `ok`, `degraded` ou `disabled`).
- **Armazenamento do banco:** card do cluster no Atlas (conferir mensalmente).

### 13.2 Notificações

1. **[PAINEL Render]** Configure notificações por e-mail para **falha de deploy** (Account/Workspace Settings → Notifications).
2. **[PAINEL Atlas]** Em **Alerts** do projeto, configure e-mail para os alertas disponíveis no M0 (conexões e uso de armazenamento) **[confirmar quais estão disponíveis no M0]**.

**Verificação:** os dois painéis mostram os destinatários configurados.

### 13.3 Limites e como perceber

| Situação | O que acontece | Como perceber | O que fazer |
|---|---|---|---|
| Banco perto de 0,5 GB | O Atlas **reduz a velocidade** (throttling) das operações | Card do cluster; alerta | Improvável no MVP. Revisar dados antigos ou migrar de plano |
| Mais de 100 operações/s ou 500 conexões | Limites do M0 | Lentidão e erros nos logs | O pool de 10 e 1 instância ficam bem abaixo |
| Mais de 10 GB de entrada ou saída em 7 dias (Atlas) | Limite do M0 | Alertas e métricas | Improvável para o volume do MVP |
| 750 horas/mês do Render esgotadas (por workspace) | O Render **suspende todos os serviços gratuitos** do workspace até o mês seguinte | Aviso do Render; API fora do ar | Manter **um só** serviço gratuito sempre disponível; não criar ambientes extras gratuitos sem necessidade |
| Render reinicia o serviço | Pode acontecer a qualquer momento | Logs | Nada: a API não guarda estado em memória que importe; o Key Value perde só cache e contadores. Um e-mail de "esqueci a senha" em envio naquele instante pode se perder; a pessoa pede de novo |
| 30 dias sem nenhuma conexão ao M0 | O Atlas **pausa** o cluster | E-mail do Atlas; `ready` em 503 | **[PAINEL Atlas]** abrir o cluster → **Resume**; esperar ficar ativo e conferir `/api/health/ready` |

---

## 14. Compensações se for preciso liberar `0.0.0.0/0`

Só em último caso e pelo menor tempo possível. Se for inevitável:

1. Senha **aleatória longa e exclusiva** no `oleoapp_api` (`openssl rand -hex 24`).
2. Usuário só com `readWrite@oleoapp`; **nenhum** outro usuário amplo no projeto.
3. TLS (o Atlas já impõe).
4. **Rotação imediata** da senha ([seção 10](#10-rotação-de-senha-do-oleoapp_api)) assim que voltar às faixas do Render.
5. Entrada `0.0.0.0/0` com **comentário e data** (ex.: `TEMPORÁRIO <motivo> <data>`), de preferência como entrada temporária.
6. Voltar para as faixas do Render ([passo 6](#6-painel-render--atlas-ips-de-saída)) assim que possível e remover a entrada.
7. Acompanhar o **Activity Feed** do Atlas no período.
8. Manter o backup em dia ([seção 12](#12-backups-e-restauração)).

---

## 15. Registro do deploy

Preencha à medida que os passos forem feitos. Nada de senha, chave ou URI com credencial aqui.

| Item | Valor |
|---|---|
| Região do Render (API e Key Value) | `<regiao>` |
| Provedor e região do Atlas | `<provedor>` / `<regiao>` |
| Nome do cluster | `<cluster>` |
| Nome do serviço no Render | `<servico>` |
| Data do primeiro deploy | `<AAAA-MM-DD>` |
| Decisão sobre `TRUSTED_PROXY_HOPS` após a FE-10 | `<1 ou 2, motivo, data>` |
| Última rotação de senha do `oleoapp_api` | `<AAAA-MM-DD>` |
| Último backup | `<AAAA-MM-DD>` |
| Último teste de restauração | `<AAAA-MM-DD>` |

## Primeiro administrador

Depois do primeiro deploy, crie o admin uma vez, a partir de uma máquina com acesso ao Atlas (o mesmo `MONGO_URL` da API):

```bash
MONGO_URL="<string do Atlas>" python -m scripts.create_admin --email voce@exemplo.com
```

O script pede a senha no terminal (ou lê `ADMIN_PASSWORD`, só nesse comando). Não cadastre `ADMIN_PASSWORD` no painel do Render.

Se `AUDIT_LOG_RETENTION_DAYS` mudar depois que o índice existir, a API tenta ajustar o TTL sozinha (`collMod`). Sem essa permissão no usuário do banco, ela registra um aviso; ajuste o índice `created_at_1` de `audit_logs` pelo Atlas.

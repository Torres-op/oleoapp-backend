# Login com Google

O ÓleoApp aceita cadastro e login com a conta do Google. O botão só aparece no site quando as credenciais estão configuradas no backend. Sem elas, o resto do app funciona normalmente.

## Como funciona

1. O usuário clica em "Continuar com Google".
2. O navegador vai para a página do Google, onde a pessoa escolhe a conta e autoriza.
3. O Google devolve a pessoa para o ÓleoApp, que cria a conta (ou entra na existente) e abre o mapa.

O endereço de retorno é sempre `<FRONTEND_URL>/api/auth/oauth/google/callback`. Ele passa pelo frontend (Vercel), que repassa a chamada ao backend. Por isso usa o domínio do **frontend**, e não o do Render.

| Ambiente | Endereço de retorno |
|---|---|
| Local | `http://localhost:3000/api/auth/oauth/google/callback` |
| Produção | `https://<projeto>.vercel.app/api/auth/oauth/google/callback` |

## Google

Qualquer conta Google pode criar um projeto gratuito. Ele pode ser separado do projeto da chave do mapa.

1. Acesse <https://console.cloud.google.com/> e crie um projeto (por exemplo, "ÓleoApp Login").
2. Em **APIs e serviços → Tela de consentimento OAuth**:
   - tipo de usuário: **Externo**;
   - nome do app: ÓleoApp; e-mail de suporte; logotipo (opcional);
   - domínios autorizados: o domínio da Vercel (por exemplo, `vercel.app` ou o domínio próprio);
   - link da política de privacidade: `<FRONTEND_URL>/privacidade`;
   - escopos: `openid`, `email` e `profile` (são os básicos, não exigem verificação do Google);
   - enquanto o app estiver em **Teste**, só os e-mails cadastrados em "Usuários de teste" conseguem entrar. Para liberar a todos, clique em **Publicar app**.
3. Em **APIs e serviços → Credenciais → Criar credenciais → ID do cliente OAuth**:
   - tipo de aplicativo: **Aplicativo da Web**;
   - **URIs de redirecionamento autorizados**: os endereços de retorno da tabela acima (local e produção).
4. Copie o **ID do cliente** e a **chave secreta do cliente** para as variáveis abaixo.

## Variáveis do backend

No `.env` local e no painel do Render:

```
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
```

`FRONTEND_URL` precisa ser exatamente o endereço do site, porque é usado para montar o endereço de retorno. Depois de mudar as variáveis, reinicie a API (`docker compose up -d` no local, ou um novo deploy no Render).

Para conferir: `GET /api/auth/providers` responde `{"google": true}` quando está configurado.

## Regras das contas

- **E-mail novo**: cria a conta sem senha, com o nome que vem do Google.
- **E-mail que já tem conta**:
  - e-mail verificado pelo Google (o normal): liga o Google à conta existente e entra;
  - e-mail não verificado: o site pede para entrar uma vez com a senha. Depois disso, o Google fica ligado à conta.
- **Conta sem senha** (criada só com o Google):
  - pode criar uma senha no perfil, se quiser;
  - para trocar o e-mail, precisa criar uma senha antes;
  - para excluir a conta, confirma digitando o próprio e-mail;
  - "Esqueci minha senha" também funciona e serve para criar a primeira senha.

## Erros que o usuário pode ver

O backend devolve a pessoa para `/login?erro=<código>`:

| Código | Quando |
|---|---|
| `oauth_cancelado` | A pessoa cancelou ou negou a autorização no Google. |
| `oauth_falhou` | O pedido expirou (mais de 10 minutos), foi alterado, ou o Google falhou. |
| `oauth_sem_email` | O Google não devolveu o e-mail. |
| `oauth_vincular` | Já existe conta com esse e-mail não verificado: entre com a senha para ligar o Google (`&provedor=google`). |
| `oauth_indisponivel` | O login com Google não está configurado no backend. |

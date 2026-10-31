from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import RedirectResponse

from app.api.deps import CurrentUserDep, Db, HttpClient, RedisDep, RequestCtx, rate_limit
from app.core.config import settings
from app.core.errors import AppError
from app.core.rate_limit import client_ip, enforce
from app.schemas.auth import (
    ChangeEmailIn,
    ChangePasswordIn,
    DeleteAccountIn,
    LoginIn,
    SetPasswordIn,
    SignupIn,
    UpdateMeIn,
    UserOut,
)
from app.services import audit
from app.services import auth as auth_service
from app.services import oauth as oauth_service

router = APIRouter(prefix="/auth", tags=["auth"])

signup_rate_limit = rate_limit("signup", "RATE_LIMIT_SIGNUP_PER_HOUR", 3600, by="ip")


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        settings.SESSION_COOKIE_NAME,
        token,
        max_age=settings.SESSION_TTL_DAYS * 86400,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        settings.SESSION_COOKIE_NAME,
        path="/",
        secure=settings.COOKIE_SECURE,
        httponly=True,
        samesite="lax",
    )


def _set_short_cookie(response: Response, name: str, value: str, path: str) -> None:
    """Cookie HttpOnly de 10 minutos usado só durante o login social."""
    response.set_cookie(
        name,
        value,
        max_age=600,
        httponly=True,
        secure=settings.COOKIE_SECURE,
        samesite="lax",
        path=path,
    )


def _clear_short_cookie(response: Response, name: str, path: str) -> None:
    response.delete_cookie(
        name, path=path, secure=settings.COOKIE_SECURE, httponly=True, samesite="lax"
    )


@router.post(
    "/signup",
    status_code=201,
    response_model=UserOut,
    dependencies=[Depends(signup_rate_limit)],
)
async def signup(data: SignupIn, response: Response, db: Db, ctx: RequestCtx) -> UserOut:
    user, token = await auth_service.signup(db, data)
    _set_session_cookie(response, token)
    await audit.record(db, "signup", ctx, user_id=user.id, email=user.email, method="password")
    return user


@router.post("/login", response_model=UserOut)
async def login(
    data: LoginIn,
    request: Request,
    response: Response,
    db: Db,
    redis: RedisDep,
    ctx: RequestCtx,
) -> UserOut:
    try:
        await enforce(
            redis,
            f"rl:login:{client_ip(request)}:{data.email}",
            settings.RATE_LIMIT_LOGIN_PER_MIN,
            60,
        )
        user, token = await auth_service.login(db, data)
    except AppError as exc:
        # E-mail digitado vai mascarado; o motivo é o código do erro (sem a senha).
        await audit.record(
            db,
            "login_failure",
            ctx,
            email=data.email,
            success=False,
            reason=exc.code,
            method="password",
        )
        raise
    _set_session_cookie(response, token)
    await audit.record(
        db, "login_success", ctx, user_id=user.id, email=user.email, method="password"
    )

    # Login social pendente (e-mail não confirmado pelo provedor): a senha
    # certa prova que a conta é desta pessoa, então o provedor é vinculado agora.
    link_token = request.cookies.get(oauth_service.LINK_COOKIE)
    if link_token:
        linked = await oauth_service.complete_pending_link(db, link_token, user.id)
        if linked is not None:
            user = linked
        _clear_short_cookie(response, oauth_service.LINK_COOKIE, oauth_service.LINK_COOKIE_PATH)
    return user


@router.post("/logout", status_code=204)
async def logout(request: Request, response: Response, db: Db, ctx: RequestCtx) -> None:
    # Funciona com ou sem sessão: apaga a sessão se existir e sempre limpa o cookie.
    user_id = await auth_service.logout(db, request.cookies.get(settings.SESSION_COOKIE_NAME))
    _clear_session_cookie(response)
    if user_id is not None:
        await audit.record(db, "logout", ctx, user_id=user_id)


@router.get("/me", response_model=UserOut)
async def get_me(user: CurrentUserDep, db: Db) -> UserOut:
    return await auth_service.get_me(db, user.id)


@router.patch("/me", response_model=UserOut)
async def update_me(data: UpdateMeIn, user: CurrentUserDep, db: Db) -> UserOut:
    return await auth_service.update_me(db, user.id, data)


@router.put("/me/email", response_model=UserOut)
async def change_email(data: ChangeEmailIn, user: CurrentUserDep, db: Db) -> UserOut:
    return await auth_service.change_email(db, user.id, data)


@router.put("/me/password", status_code=204)
async def change_password(
    data: ChangePasswordIn, request: Request, user: CurrentUserDep, db: Db
) -> None:
    current_token = request.cookies.get(settings.SESSION_COOKIE_NAME)
    await auth_service.change_password(db, user.id, data, current_token)


@router.post("/set-password", response_model=UserOut)
async def set_password(data: SetPasswordIn, user: CurrentUserDep, db: Db) -> UserOut:
    return await auth_service.set_password(db, user.id, data)


@router.post("/delete-account", status_code=204)
async def delete_account(
    data: DeleteAccountIn, response: Response, user: CurrentUserDep, db: Db, ctx: RequestCtx
) -> None:
    await auth_service.delete_account(db, user.id, data)
    _clear_session_cookie(response)
    # O evento fica registrado, mas todos os eventos da pessoa perdem id e e-mail (LGPD).
    await audit.record(db, "account_deleted", ctx, user_id=user.id, email=user.email)
    await audit.anonymize_user(db, user.id)


# --- login social (Google) --------------------------------------------------


@router.get("/providers")
async def providers() -> dict[str, bool]:
    """Quais botões de login social o frontend deve mostrar."""
    return oauth_service.providers_status()


@router.get("/oauth/{provider}/start")
async def oauth_start(
    provider: str,
    db: Db,
    next_path: Annotated[str | None, Query(alias="next", max_length=512)] = None,
) -> RedirectResponse:
    if not oauth_service.is_configured(provider):
        return RedirectResponse(
            oauth_service.frontend_url("/login?erro=oauth_indisponivel"), status_code=302
        )
    url, state = await oauth_service.start(db, provider, next_path)
    response = RedirectResponse(url, status_code=302)
    _set_short_cookie(response, oauth_service.STATE_COOKIE, state, oauth_service.STATE_COOKIE_PATH)
    return response


@router.get("/oauth/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    db: Db,
    http: HttpClient,
    ctx: RequestCtx,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Volta do provedor. Sempre redireciona: para `next` (sucesso) ou /login?erro=<código>."""
    outcome = await oauth_service.callback(
        db,
        http,
        provider,
        code=code,
        state=state,
        error=error,
        cookie_state=request.cookies.get(oauth_service.STATE_COOKIE),
    )

    if outcome.error:
        path = f"/login?erro={outcome.error}"
        if outcome.error == "oauth_vincular":
            path += f"&provedor={provider}"
    else:
        path = outcome.next_path
    response = RedirectResponse(oauth_service.frontend_url(path), status_code=302)
    _clear_short_cookie(response, oauth_service.STATE_COOKIE, oauth_service.STATE_COOKIE_PATH)

    if outcome.session_token:
        _set_session_cookie(response, outcome.session_token)
        if outcome.created:
            await audit.record(
                db, "signup", ctx, user_id=outcome.user_id, email=outcome.email, method="google"
            )
        await audit.record(
            db,
            "login_success",
            ctx,
            user_id=outcome.user_id,
            email=outcome.email,
            method="google",
        )
    elif outcome.error:
        disabled = outcome.error == oauth_service.ACCOUNT_DISABLED
        await audit.record(
            db,
            "login_failure",
            ctx,
            email=outcome.email,
            success=False,
            reason="account_disabled" if disabled else outcome.error,
            method="google",
        )
    if outcome.link_token:
        _set_short_cookie(
            response,
            oauth_service.LINK_COOKIE,
            outcome.link_token,
            oauth_service.LINK_COOKIE_PATH,
        )
    return response

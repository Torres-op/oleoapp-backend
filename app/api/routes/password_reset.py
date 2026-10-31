import hashlib

from fastapi import APIRouter, Depends

from app.api.deps import Db, HttpClient, RedisDep, check_rate_limit, rate_limit
from app.schemas.password_reset import ForgotPasswordIn, ResetPasswordIn
from app.services import password_reset as password_reset_service

router = APIRouter(prefix="/auth/password", tags=["auth"])

forgot_rate_limit = rate_limit("pwd_forgot", "RATE_LIMIT_PASSWORD_FORGOT_PER_HOUR", 3600, by="ip")


@router.post("/forgot", status_code=204, dependencies=[Depends(forgot_rate_limit)])
async def forgot(data: ForgotPasswordIn, db: Db, http: HttpClient, redis: RedisDep) -> None:
    # Sempre 204, exista ou não o e-mail. O limite por e-mail vale para os dois casos
    # (a chave é o hash do texto informado), então a resposta não revela se a conta existe.
    email_key = hashlib.sha1(data.email.encode(), usedforsecurity=False).hexdigest()
    await check_rate_limit(
        redis, "pwd_forgot_email", "RATE_LIMIT_PASSWORD_FORGOT_PER_EMAIL_PER_HOUR", 3600, email_key
    )
    # Responde na hora: o resto (usuário, token e e-mail) roda em segundo plano.
    password_reset_service.schedule_forgot(db, http, data.email)


@router.post("/reset", status_code=204)
async def reset(data: ResetPasswordIn, db: Db) -> None:
    await password_reset_service.reset(db, data.token, data.new_password)

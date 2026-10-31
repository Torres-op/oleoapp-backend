import re
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

PASSWORD_POLICY_MESSAGE = (
    "A senha deve ter de 8 a 128 caracteres, com pelo menos uma letra maiúscula e um número."
)


def validate_password_policy(password: str) -> str:
    """Política de senha (8 a 128, ≥ 1 maiúscula, ≥ 1 número). Reutilizada pela BE-11."""
    if (
        not 8 <= len(password) <= 128
        or not any(char.isupper() for char in password)
        or not re.search(r"[0-9]", password)
    ):
        raise ValueError(PASSWORD_POLICY_MESSAGE)
    return password


def _normalize_email(value: str) -> str:
    value = value.strip().lower()
    if len(value) > 254:
        raise ValueError("O e-mail deve ter no máximo 254 caracteres.")
    return value


Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=80)]
Bairro = Annotated[str, StringConstraints(strip_whitespace=True, max_length=60)]
Email = Annotated[EmailStr, AfterValidator(_normalize_email)]
Password = Annotated[str, AfterValidator(validate_password_policy)]
CurrentPassword = Annotated[str, Field(min_length=1, max_length=128)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SignupIn(StrictModel):
    name: Name
    email: Email
    password: Password


class LoginIn(StrictModel):
    email: Email
    password: CurrentPassword


class UpdateMeIn(StrictModel):
    name: Name | None = None
    bairro: Bairro | None = None
    reminder_interval_days: Literal[90, 180, 365] | None = None

    @field_validator("name", "reminder_interval_days")
    @classmethod
    def _not_null(cls, value):
        if value is None:
            raise ValueError("Este campo não pode ser nulo.")
        return value

    @field_validator("bairro")
    @classmethod
    def _blank_becomes_null(cls, value: str | None) -> str | None:
        return value or None

    @model_validator(mode="after")
    def _at_least_one_field(self) -> Self:
        if not self.model_fields_set:
            raise ValueError("Informe pelo menos um campo para atualizar.")
        return self


class ChangeEmailIn(StrictModel):
    new_email: Email
    current_password: CurrentPassword


class ChangePasswordIn(StrictModel):
    current_password: CurrentPassword
    new_password: Password

    @model_validator(mode="after")
    def _different_from_current(self) -> Self:
        if self.new_password == self.current_password:
            raise ValueError("A nova senha deve ser diferente da atual.")
        return self


class SetPasswordIn(StrictModel):
    """Primeira senha de uma conta criada só com login social."""

    new_password: Password


class DeleteAccountIn(StrictModel):
    """Conta com senha confirma com current_password; conta sem senha, com o próprio e-mail."""

    current_password: CurrentPassword | None = None
    confirm_email: Annotated[str, Field(max_length=254)] | None = None


class UserOut(BaseModel):
    id: str
    name: str
    email: str
    bairro: str | None
    reminder_interval_days: int
    created_at: datetime
    has_password: bool
    providers: list[str]
    role: Literal["user", "admin"] = "user"

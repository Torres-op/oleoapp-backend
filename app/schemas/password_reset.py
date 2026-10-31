from typing import Annotated

from pydantic import AfterValidator, Field

from app.schemas.auth import Email, StrictModel, validate_password_policy


class ForgotPasswordIn(StrictModel):
    email: Email


class ResetPasswordIn(StrictModel):
    token: Annotated[str, Field(min_length=20, max_length=200)]
    new_password: Annotated[str, AfterValidator(validate_password_policy)]

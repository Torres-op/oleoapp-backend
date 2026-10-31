from pydantic import BaseModel, ConfigDict, Field


class LatLng(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class ErrorField(BaseModel):
    field: str
    message: str


class ErrorBody(BaseModel):
    code: str
    message: str
    fields: list[ErrorField] | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody

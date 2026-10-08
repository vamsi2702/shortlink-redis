from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from api.services.urls import validate_url


class ShortenRequest(BaseModel):
    url: str

    @field_validator("url")
    @classmethod
    def url_must_be_safe(cls, value: str) -> str:
        return validate_url(value)


class UpdateLinkRequest(ShortenRequest):
    """Same rules as creating a link: the new target URL must pass validation."""


class LinkOut(BaseModel):
    code: str
    short_url: str
    long_url: str
    created_at: datetime


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)

    @field_validator("password")
    @classmethod
    def password_fits_bcrypt(cls, value: str) -> str:
        # bcrypt only looks at the first 72 bytes, so longer passwords are refused, not truncated
        if len(value.encode()) > 72:
            raise ValueError("password must be at most 72 bytes")
        return value


class LoginRequest(BaseModel):
    email: str
    password: str


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"

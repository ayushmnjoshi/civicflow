"""Pydantic schemas for authentication endpoints.

password_hash is deliberately absent from every schema.
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


# ── request schemas ───────────────────────────────────────────────────────────

class RegisterRequest(BaseModel):
    """POST /auth/register body.

    The ``role`` field is intentionally absent — the server always assigns
    'citizen'.  Extra fields sent by the client are silently ignored (Pydantic
    v2 default behaviour).
    """

    email: EmailStr
    password: str = Field(min_length=8, description="At least 8 characters.")
    full_name: str = Field(min_length=1)
    phone: str | None = None


class LoginRequest(BaseModel):
    """POST /auth/login body."""

    email: EmailStr
    password: str


# ── response schemas ──────────────────────────────────────────────────────────

class RegisterResponse(BaseModel):
    """201 response from POST /auth/register."""

    user_id: int
    email: str
    full_name: str
    role: str


class TokenResponse(BaseModel):
    """200 response from POST /auth/login."""

    access_token: str
    token_type: str = "bearer"
    role: str


class CurrentUserResponse(BaseModel):
    """200 response from GET /auth/me."""

    id: int
    email: str
    full_name: str
    role: str

"""Auth router — register, login, me."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.dependencies import get_current_user
from app.auth.security import create_access_token, hash_password, verify_password
from app.config import settings
from app.constants import AuditAction, AuditEntity
from app.dependencies import get_db
from app.models.role import Role
from app.models.user import User
from app.schemas.auth import (
    CurrentUserResponse,
    LoginRequest,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
)
from app.services.audit_service import record_standalone as audit_standalone

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth", tags=["auth"])

# Pre-computed bcrypt hash used as a stand-in when the user doesn't exist so
# verify_password() is always called and response timing stays uniform.
# Generated with rounds=4 (fast enough, only used as a timing dummy).
_DUMMY_HASH = "$2b$04$oWeIPNNewIvzIMA7Q4x/TOqFzU6rQZZ4b.64bZ/x5YMvarTqfa49a"


# ── helpers ───────────────────────────────────────────────────────────────────

def _get_role_name(db: Session, role_id: int) -> str:
    role = db.get(Role, role_id)
    return role.name if role else "unknown"


# ── POST /auth/register ───────────────────────────────────────────────────────

@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    response_model=RegisterResponse,
    summary="Register a new citizen account",
)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> RegisterResponse:
    """
    Creates a new user.

    - Role is always **citizen** — the request body cannot override this.
    - Email is normalised to lowercase before storage.
    - Returns **409** if the email is already registered.
    - Returns **422** if the password is shorter than 8 characters.
    """
    email = payload.email.lower()

    # Resolve the citizen role
    citizen_role = db.query(Role).filter(Role.name == "citizen").first()
    if citizen_role is None:
        logger.error("'citizen' role not found in DB — run the seed script.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server configuration error.",
        )

    user = User(
        email=email,
        password_hash=hash_password(payload.password),
        role_id=citizen_role.id,
        full_name=payload.full_name,
        phone=payload.phone,
    )
    db.add(user)

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    db.refresh(user)
    logger.info("New citizen registered: id=%d email=%s", user.id, email)

    # Audit: auth.register (standalone so a write failure never blocks the response)
    try:
        audit_standalone(
            db,
            user_id=user.id,
            action=AuditAction.AUTH_REGISTER,
            entity_type=AuditEntity.USER,
            entity_id=user.id,
        )
    except Exception:
        logger.exception("audit_callsite_failed action=auth.register user_id=%d", user.id)

    return RegisterResponse(
        user_id=user.id,
        email=user.email,
        full_name=user.full_name,
        role="citizen",
    )


# ── POST /auth/login ──────────────────────────────────────────────────────────

@router.post(
    "/login",
    response_model=TokenResponse,
    summary="Obtain a JWT access token",
)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """
    Authenticates with email + password.

    Returns the **identical** 401 for a wrong email and a wrong password so
    attackers cannot enumerate which accounts exist.
    """
    _INVALID = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password.",
        headers={"WWW-Authenticate": "Bearer"},
    )

    user = db.query(User).filter(User.email == payload.email.lower()).first()

    # Always run verify_password even when the user doesn't exist so the
    # response time is indistinguishable from a wrong-password case.
    stored_hash = user.password_hash if user else _DUMMY_HASH
    password_ok = verify_password(payload.password, stored_hash)

    # System user has an unusable hash — login always fails regardless.
    # Explicitly check role name so the behaviour is guaranteed even if somehow
    # the hash format changed.
    is_system = False
    if user is not None:
        _user_role = _get_role_name(db, user.role_id)
        if _user_role == "system":
            password_ok = False
            is_system = True

    if user is None or not password_ok:
        # Audit: login failure — only for known users (unknown email writes nothing)
        if user is not None:
            _failure_reason = "system_account" if is_system else "bad_password"
            try:
                audit_standalone(
                    db,
                    user_id=user.id,
                    action=AuditAction.AUTH_LOGIN_FAILURE,
                    entity_type=AuditEntity.USER,
                    entity_id=user.id,
                    details={"reason": _failure_reason},
                )
            except Exception:
                logger.exception("audit_callsite_failed action=auth.login_failure user_id=%d", user.id)
        else:
            logger.info("login_failure reason_code=unknown_email")
        raise _INVALID

    # Audit: login success (standalone so audit failure never blocks)
    try:
        audit_standalone(
            db,
            user_id=user.id,
            action=AuditAction.AUTH_LOGIN_SUCCESS,
            entity_type=AuditEntity.USER,
            entity_id=user.id,
        )
    except Exception:
        logger.exception("audit_callsite_failed action=auth.login_success user_id=%d", user.id)

    token = create_access_token(
        user_id=user.id,
        secret=settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
        expire_minutes=settings.access_token_expire_minutes,
    )

    return TokenResponse(
        access_token=token,
        token_type="bearer",
        role=_get_role_name(db, user.role_id),
    )


# ── GET /auth/me ──────────────────────────────────────────────────────────────

@router.get(
    "/me",
    response_model=CurrentUserResponse,
    summary="Return the currently authenticated user",
)
def me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> CurrentUserResponse:
    """Requires a valid Bearer token."""
    return CurrentUserResponse(
        id=current_user.id,
        email=current_user.email,
        full_name=current_user.full_name,
        role=_get_role_name(db, current_user.role_id),
    )

"""
Auth API
========
  POST /auth/register  — create a new user account
  POST /auth/login     — authenticate, returns JWT access token
  GET  /auth/me        — return the currently authenticated user
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select, func
from loguru import logger

from db.database import AsyncSessionLocal
from db.models import User
from core.auth import hash_password, verify_password, create_access_token, get_current_user, audit

router = APIRouter()

# ── Simple in-process login rate limiter ─────────────────────────────────────
# Limits each IP to _LOGIN_MAX_ATTEMPTS failed attempts within _LOGIN_WINDOW seconds.
import collections, time as _time
_LOGIN_WINDOW = 300       # 5-minute sliding window
_LOGIN_MAX_ATTEMPTS = 10  # max failed attempts per IP in that window
_login_attempts: dict[str, list[float]] = collections.defaultdict(list)


# ── Request / response schemas ───────────────────────────────────────────────

class RegisterRequest(BaseModel):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def _clean_username(cls, v: str) -> str:
        v = v.strip().lower()
        if len(v) < 3:
            raise ValueError("Username must be at least 3 characters")
        if len(v) > 50:
            raise ValueError("Username must be 50 characters or fewer")
        return v

    @field_validator("password")
    @classmethod
    def _strong_password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        return v


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    is_admin: bool


class UserResponse(BaseModel):
    id: int
    username: str
    is_admin: bool


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/register", response_model=UserResponse, status_code=201)
async def register(payload: RegisterRequest):
    """Create a new user account. The first registered user is automatically admin."""
    async with AsyncSessionLocal() as session:
        # Check username uniqueness
        existing = await session.execute(select(User).where(User.username == payload.username))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Username already taken")

        # Atomic user-count check — avoids TOCTOU race that would produce two admins.
        # func.count() runs a single SELECT COUNT(*) in the same transaction,
        # and the unique constraint on `username` prevents any duplicate commit.
        count_result = await session.execute(select(func.count()).select_from(User))
        is_first = count_result.scalar() == 0

        user = User(
            username=payload.username,
            hashed_password=hash_password(payload.password),
            is_admin=is_first,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        logger.info(f"[Auth] New user registered: {user.username} (admin={user.is_admin})")
        audit("user.register", actor=user.username, admin=user.is_admin)

    return UserResponse(id=user.id, username=user.username, is_admin=user.is_admin)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest, request: Request):
    """Authenticate with username + password, returns a 7-day JWT."""
    client_ip = request.client.host if request.client else "unknown"

    # Rate-limit check — sliding window per source IP
    now = _time.monotonic()
    attempts = _login_attempts[client_ip]
    # Evict attempts outside the window
    _login_attempts[client_ip] = [t for t in attempts if now - t < _LOGIN_WINDOW]
    if len(_login_attempts[client_ip]) >= _LOGIN_MAX_ATTEMPTS:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many login attempts. Please try again later.",
        )

    username = payload.username.strip().lower()

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()

    if not user or not verify_password(payload.password, user.hashed_password):
        # Record failed attempt for rate limiting
        _login_attempts[client_ip].append(_time.monotonic())
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    token = create_access_token(subject=user.username)
    logger.info(f"[Auth] Login: {user.username}")
    audit("user.login", actor=user.username, ip=client_ip)
    return TokenResponse(
        access_token=token,
        token_type="bearer",
        username=user.username,
        is_admin=user.is_admin,
    )


@router.get("/me", response_model=UserResponse)
async def me(current_user: User = Depends(get_current_user)):
    """Return info on the currently authenticated user."""
    return UserResponse(
        id=current_user.id,
        username=current_user.username,
        is_admin=current_user.is_admin,
    )

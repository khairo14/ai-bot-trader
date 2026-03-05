"""
Auth API
========
  POST /auth/register  — create a new user account
  POST /auth/login     — authenticate, returns JWT access token
  GET  /auth/me        — return the currently authenticated user
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from loguru import logger

from db.database import AsyncSessionLocal
from db.models import User
from core.auth import hash_password, verify_password, create_access_token, get_current_user

router = APIRouter()


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

        # First user ever → admin
        count_r = await session.execute(select(User))
        is_first = len(count_r.scalars().all()) == 0

        user = User(
            username=payload.username,
            hashed_password=hash_password(payload.password),
            is_admin=is_first,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        logger.info(f"[Auth] New user registered: {user.username} (admin={user.is_admin})")

    return UserResponse(id=user.id, username=user.username, is_admin=user.is_admin)


@router.post("/login", response_model=TokenResponse)
async def login(payload: LoginRequest):
    """Authenticate with username + password, returns a 7-day JWT."""
    username = payload.username.strip().lower()

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()

    if not user or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
        )
    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account disabled")

    token = create_access_token(subject=user.username)
    logger.info(f"[Auth] Login: {user.username}")
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

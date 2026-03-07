"""
JWT authentication helpers and FastAPI dependency.

Flow:
  - Passwords are SHA-256 pre-hashed then bcrypt-hashed directly (no passlib —
    passlib is incompatible with bcrypt >= 4.0)
  - Tokens are signed HS256 JWTs using SECRET_KEY from settings
  - get_current_user() is a FastAPI Depends() injected on every protected router
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from loguru import logger

from config import settings

ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7   # 7 days

# tokenUrl must match the login endpoint path — used by Swagger UI "Authorize"
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


# ── Password helpers ─────────────────────────────────────────────────────────

def _prehash(password: str) -> bytes:
    """SHA-256 digest of the password avoids bcrypt's 72-byte limit entirely.
    Returns raw bytes suitable for bcrypt.hashpw()."""
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    """Return a bcrypt hash string for storage."""
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Return True if plain matches the stored bcrypt hash."""
    return bcrypt.checkpw(_prehash(plain), hashed.encode("utf-8"))


# ── JWT helpers ──────────────────────────────────────────────────────────────

def create_access_token(subject: str, expires_delta: Optional[timedelta] = None) -> str:
    """Create a signed JWT with `sub` = username."""
    expire = datetime.now(timezone.utc) + (expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))
    payload = {"sub": subject, "exp": expire}
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


# ── FastAPI dependency ───────────────────────────────────────────────────────

async def get_current_user(request: Request, token: str = Depends(oauth2_scheme)):
    """
    Resolve a Bearer token (Authorization header) or httpOnly cookie to a User row.
    Cookie name: 'access_token' (F-056). Header takes precedence for API clients.
    Raises HTTP 401 if the token is missing, invalid, or expired.
    """
    # Prefer Authorization header (API / Swagger); fall back to httpOnly cookie
    cookie_token: Optional[str] = request.cookies.get("access_token")
    resolved_token = token if token else cookie_token
    from db.database import AsyncSessionLocal
    from db.models import User
    from sqlalchemy import select

    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        if not resolved_token:
            raise credentials_exc
        payload = jwt.decode(resolved_token, settings.secret_key, algorithms=[ALGORITHM])
        username: Optional[str] = payload.get("sub")
        if not username:
            raise credentials_exc
    except JWTError:
        raise credentials_exc

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.username == username))
        user = result.scalar_one_or_none()

    if user is None or not user.is_active:
        raise credentials_exc

    return user


async def require_admin(user=Depends(get_current_user)):
    """Dependency that raises HTTP 403 for non-admin users."""
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


def audit(action: str, actor: str = "system", **details) -> None:
    """
    Write a structured audit-log entry.
    Output goes to the application log at INFO level with a recognisable prefix
    so it can be filtered independently (e.g. grep AUDIT or shipped to SIEM).
    """
    extras = " | ".join(f"{k}={v}" for k, v in details.items())
    logger.info(f"[AUDIT] action={action} actor={actor}{' | ' + extras if extras else ''}")

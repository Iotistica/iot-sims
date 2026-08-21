"""Authentication and token helpers.

Physically extracted from src/legacy.py -- continuing the GH #15 refactor,
same "moved verbatim, no behavior changes" standard as the Database and
simulation-engine extractions.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import HTTPException, Request

from .. import dependencies
from .config import JWT_ALGORITHM, _get_jwt_secret


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user_id: int, username: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user_id),
        "username": username,
        "iat": now,
        "exp": now + timedelta(hours=dependencies.JWT_EXPIRE_HOURS),
    }
    return jwt.encode(payload, _get_jwt_secret(), algorithm=JWT_ALGORITHM)


def decode_access_token(token: str) -> Optional[dict]:
    try:
        return jwt.decode(token, _get_jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError:
        return None


def user_from_token(token: str) -> Optional[dict]:
    """Decode a token and re-fetch the user row, so a deleted user's old
    token stops working immediately rather than staying valid until expiry."""
    payload = decode_access_token(token)
    if not payload:
        return None
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        return None
    user = dependencies.db.get_user(user_id)
    if not user:
        return None
    return {"id": user["id"], "username": user["username"]}


def get_current_user(request: Request) -> dict:
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = user_from_token(auth_header[7:].strip())
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired session")
    return user


__all__ = [
    "hash_password", "verify_password", "create_access_token",
    "decode_access_token", "user_from_token", "get_current_user",
]

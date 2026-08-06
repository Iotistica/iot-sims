from __future__ import annotations

import asyncio
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet.schemas import (
    Credentials,
    PasswordReset,
)


router = APIRouter(tags=["authentication"])


PasswordHasher = Callable[[str], str]
PasswordVerifier = Callable[[str, str], bool]
TokenCreator = Callable[[int, str], str]
CurrentUserResolver = Callable[[Request], dict]


# ─── Runtime dependencies ─────────────────────────────────────────────────────

def get_database(request: Request) -> Any:
    database = getattr(
        request.app.state,
        "db",
        None,
    )

    if database is None:
        raise HTTPException(
            status_code=503,
            detail="Database is unavailable",
        )

    return database


def get_password_hasher(
    request: Request,
) -> PasswordHasher:
    callback = getattr(
        request.app.state,
        "hash_password",
        None,
    )

    if callback is None:
        raise HTTPException(
            status_code=503,
            detail="Authentication service is unavailable",
        )

    return callback


def get_password_verifier(
    request: Request,
) -> PasswordVerifier:
    callback = getattr(
        request.app.state,
        "verify_password",
        None,
    )

    if callback is None:
        raise HTTPException(
            status_code=503,
            detail="Authentication service is unavailable",
        )

    return callback


def get_token_creator(
    request: Request,
) -> TokenCreator:
    callback = getattr(
        request.app.state,
        "create_access_token",
        None,
    )

    if callback is None:
        raise HTTPException(
            status_code=503,
            detail="Authentication service is unavailable",
        )

    return callback


def resolve_current_user(
    request: Request,
) -> dict:
    resolver: CurrentUserResolver | None = getattr(
        request.app.state,
        "get_current_user",
        None,
    )

    if resolver is None:
        raise HTTPException(
            status_code=503,
            detail="Authentication service is unavailable",
        )

    return resolver(request)


def public_user(user: dict) -> dict:
    """
    Return only fields safe to expose through the API.

    Database user rows may include password_hash.
    """
    result = {
        "id": user["id"],
        "username": user["username"],
    }

    if "created_at" in user:
        result["created_at"] = user["created_at"]

    if "last_login_at" in user:
        result["last_login_at"] = user["last_login_at"]

    return result


# ─── Authentication ───────────────────────────────────────────────────────────

@router.get("/auth/setup-required")
async def setup_required(
    request: Request,
):
    database = get_database(request)

    count = await asyncio.to_thread(
        database.count_users
    )

    return {
        "setup_required": count == 0,
    }


@router.post(
    "/auth/setup",
    status_code=201,
)
async def setup_authentication(
    body: Credentials,
    request: Request,
):
    database = get_database(request)
    hash_password = get_password_hasher(request)
    create_access_token = get_token_creator(request)

    count = await asyncio.to_thread(
        database.count_users
    )

    if count > 0:
        raise HTTPException(
            status_code=409,
            detail="Setup already completed",
        )

    password_hash = await asyncio.to_thread(
        hash_password,
        body.password,
    )

    try:
        user = await asyncio.to_thread(
            database.create_user,
            body.username,
            password_hash,
        )
    except Exception as exc:
        # Protect against two setup requests racing to create
        # the first user with the same username.
        existing = await asyncio.to_thread(
            database.get_user_by_username,
            body.username,
        )

        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail="Username already exists",
            ) from exc

        raise

    token = create_access_token(
        user["id"],
        user["username"],
    )

    return {
        "access_token": token,
        "user": public_user(user),
    }


@router.post("/auth/login")
async def login(
    body: Credentials,
    request: Request,
):
    database = get_database(request)
    verify_password = get_password_verifier(request)
    create_access_token = get_token_creator(request)

    user = await asyncio.to_thread(
        database.get_user_by_username,
        body.username,
    )

    valid_password = False

    if user is not None:
        valid_password = await asyncio.to_thread(
            verify_password,
            body.password,
            user["password_hash"],
        )

    if user is None or not valid_password:
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password",
        )

    await asyncio.to_thread(
        database.touch_last_login,
        user["id"],
    )

    token = create_access_token(
        user["id"],
        user["username"],
    )

    return {
        "access_token": token,
        "user": {
            "id": user["id"],
            "username": user["username"],
        },
    }


@router.get("/auth/me")
async def get_authenticated_user(
    request: Request,
):
    return resolve_current_user(request)


# ─── User administration ──────────────────────────────────────────────────────

@router.get("/users")
async def list_users(
    request: Request,
):
    database = get_database(request)

    users = await asyncio.to_thread(
        database.list_users
    )

    return [
        public_user(user)
        for user in users
    ]


@router.post(
    "/users",
    status_code=201,
)
async def create_user(
    body: Credentials,
    request: Request,
):
    database = get_database(request)
    hash_password = get_password_hasher(request)

    existing = await asyncio.to_thread(
        database.get_user_by_username,
        body.username,
    )

    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail="Username already exists",
        )

    password_hash = await asyncio.to_thread(
        hash_password,
        body.password,
    )

    try:
        user = await asyncio.to_thread(
            database.create_user,
            body.username,
            password_hash,
        )
    except Exception as exc:
        # Handles a concurrent request creating the same username
        # after the check above.
        duplicate = await asyncio.to_thread(
            database.get_user_by_username,
            body.username,
        )

        if duplicate is not None:
            raise HTTPException(
                status_code=409,
                detail="Username already exists",
            ) from exc

        raise

    return public_user(user)


@router.post("/users/{user_id}/password")
async def reset_user_password(
    user_id: int,
    body: PasswordReset,
    request: Request,
):
    database = get_database(request)
    hash_password = get_password_hasher(request)

    user = await asyncio.to_thread(
        database.get_user,
        user_id,
    )

    if user is None:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    password_hash = await asyncio.to_thread(
        hash_password,
        body.password,
    )

    await asyncio.to_thread(
        database.update_user_password,
        user_id,
        password_hash,
    )

    return {"ok": True}


@router.delete(
    "/users/{user_id}",
    status_code=204,
)
async def delete_user(
    user_id: int,
    request: Request,
) -> Response:
    database = get_database(request)
    current_user = resolve_current_user(request)

    user = await asyncio.to_thread(
        database.get_user,
        user_id,
    )

    if user is None:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    user_count = await asyncio.to_thread(
        database.count_users
    )

    if user_count <= 1:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete the last remaining user",
        )

    # Prevent the current session from deleting its own account.
    # Remove this block to preserve the old behavior literally.
    if user_id == current_user["id"]:
        raise HTTPException(
            status_code=400,
            detail="You cannot delete your own user account",
        )

    deleted = await asyncio.to_thread(
        database.delete_user,
        user_id,
    )

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="User not found",
        )

    return Response(status_code=204)
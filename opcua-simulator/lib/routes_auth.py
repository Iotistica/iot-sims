"""Auth + user management routes — split out of opcua_simulator.py."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import lib.state as state
from lib.db import create_access_token, hash_password, verify_password

router = APIRouter()


class Credentials(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=200)


class PasswordReset(BaseModel):
    password: str = Field(..., min_length=8, max_length=200)


@router.get("/auth/setup-required")
async def auth_setup_required():
    count = await asyncio.to_thread(state.db.count_users)
    return {"setup_required": count == 0}


@router.post("/auth/setup", status_code=201)
async def auth_setup(body: Credentials):
    count = await asyncio.to_thread(state.db.count_users)
    if count > 0:
        raise HTTPException(status_code=409, detail="Setup already completed")
    password_hash = hash_password(body.password)
    user = await asyncio.to_thread(state.db.create_user, body.username, password_hash)
    token = create_access_token(user["id"], user["username"])
    return {"access_token": token, "user": user}


@router.post("/auth/login")
async def auth_login(body: Credentials):
    user = await asyncio.to_thread(state.db.get_user_by_username, body.username)
    if not user or not verify_password(body.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    await asyncio.to_thread(state.db.touch_last_login, user["id"])
    token = create_access_token(user["id"], user["username"])
    return {"access_token": token, "user": {"id": user["id"], "username": user["username"]}}


@router.get("/auth/me")
async def auth_me(current_user: dict = Depends(state.get_current_user)):
    return current_user


@router.get("/users")
async def list_users():
    return await asyncio.to_thread(state.db.list_users)


@router.post("/users", status_code=201)
async def create_user(body: Credentials):
    if await asyncio.to_thread(state.db.get_user_by_username, body.username):
        raise HTTPException(status_code=409, detail="Username already exists")
    password_hash = hash_password(body.password)
    return await asyncio.to_thread(state.db.create_user, body.username, password_hash)


@router.post("/users/{user_id}/password")
async def reset_user_password(user_id: int, body: PasswordReset):
    if not await asyncio.to_thread(state.db.get_user, user_id):
        raise HTTPException(status_code=404, detail="User not found")
    password_hash = hash_password(body.password)
    await asyncio.to_thread(state.db.update_user_password, user_id, password_hash)
    return {"ok": True}


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(user_id: int):
    if not await asyncio.to_thread(state.db.get_user, user_id):
        raise HTTPException(status_code=404, detail="User not found")
    if await asyncio.to_thread(state.db.count_users) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last remaining user")
    await asyncio.to_thread(state.db.delete_user, user_id)

"""Dashboards -- just a name to group custom_graphs (widget) rows under.
Same shape as custom_graphs.py's own router (own get_database helper,
asyncio.to_thread for every DB call). See migration 28's docstring for why
widgets live on the existing custom_graphs table rather than a new one.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet.schemas import DashboardCreate, DashboardUpdate

router = APIRouter(
    prefix="/dashboards",
    tags=["dashboards"],
)


def get_database(request: Request) -> Any:
    database = getattr(request.app.state, "db", None)
    if database is None:
        raise HTTPException(status_code=503, detail="Database is unavailable")
    return database


@router.get("")
async def list_dashboards(request: Request):
    database = get_database(request)
    return await asyncio.to_thread(database.get_dashboards)


@router.post("", status_code=201)
async def create_dashboard(body: DashboardCreate, request: Request):
    database = get_database(request)
    return await asyncio.to_thread(database.create_dashboard, body.name)


@router.get("/{dashboard_id}")
async def get_dashboard(dashboard_id: int, request: Request):
    database = get_database(request)
    dashboard = await asyncio.to_thread(database.get_dashboard, dashboard_id)
    if dashboard is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return dashboard


@router.put("/{dashboard_id}")
async def update_dashboard(dashboard_id: int, body: DashboardUpdate, request: Request):
    database = get_database(request)
    existing = await asyncio.to_thread(database.get_dashboard, dashboard_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    return await asyncio.to_thread(database.update_dashboard, dashboard_id, body.name)


@router.delete("/{dashboard_id}", status_code=204)
async def delete_dashboard(dashboard_id: int, request: Request) -> Response:
    database = get_database(request)
    existing = await asyncio.to_thread(database.get_dashboard, dashboard_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="Dashboard not found")
    await asyncio.to_thread(database.delete_dashboard, dashboard_id)
    return Response(status_code=204)

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet.schemas import CustomGraphCreate, CustomGraphUpdate


router = APIRouter(
    prefix="/custom-graphs",
    tags=["custom-graphs"],
)


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


@router.get("")
async def list_custom_graphs(
    request: Request,
):
    database = get_database(request)

    return await asyncio.to_thread(
        database.get_custom_graphs
    )


@router.post(
    "",
    status_code=201,
)
async def create_custom_graph(
    body: CustomGraphCreate,
    request: Request,
):
    database = get_database(request)

    body.validate_definition()

    return await asyncio.to_thread(
        database.create_custom_graph,
        body.model_dump(),
    )


@router.get("/{graph_id}")
async def get_custom_graph(
    graph_id: int,
    request: Request,
):
    database = get_database(request)

    graph = await asyncio.to_thread(
        database.get_custom_graph,
        graph_id,
    )

    if graph is None:
        raise HTTPException(
            status_code=404,
            detail="Custom graph not found",
        )

    return graph


@router.put("/{graph_id}")
async def update_custom_graph(
    graph_id: int,
    body: CustomGraphUpdate,
    request: Request,
):
    database = get_database(request)

    body.validate_definition()

    existing = await asyncio.to_thread(
        database.get_custom_graph,
        graph_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Custom graph not found",
        )

    updated = await asyncio.to_thread(
        database.update_custom_graph,
        graph_id,
        body.model_dump(),
    )

    return updated


@router.delete(
    "/{graph_id}",
    status_code=204,
)
async def delete_custom_graph(
    graph_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_custom_graph,
        graph_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Custom graph not found",
        )

    await asyncio.to_thread(
        database.delete_custom_graph,
        graph_id,
    )

    return Response(status_code=204)

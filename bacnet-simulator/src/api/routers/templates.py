from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from ...bacnet.schemas import TemplateCreate


router = APIRouter(
    prefix="/templates",
    tags=["templates"],
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
async def list_templates(
    request: Request,
):
    database = get_database(request)

    return await asyncio.to_thread(
        database.list_templates
    )


@router.post(
    "",
    status_code=201,
)
async def create_template(
    body: TemplateCreate,
    request: Request,
):
    database = get_database(request)

    body.validate_semantic()

    return await asyncio.to_thread(
        database.create_template,
        body.label,
        body.description,
        [o.model_dump() for o in body.objects],
        body.equipment_types,
    )


@router.delete(
    "/{template_id}",
    status_code=204,
)
async def delete_template(
    template_id: int,
    request: Request,
) -> Response:
    database = get_database(request)

    existing = await asyncio.to_thread(
        database.get_template,
        template_id,
    )

    if existing is None:
        raise HTTPException(
            status_code=404,
            detail="Template not found",
        )

    # Built-ins are seeded, delete-protected rows -- enforced here, not just
    # by TemplatePickerModal.vue hiding the delete button for them.
    if existing["is_builtin"]:
        raise HTTPException(
            status_code=400,
            detail="Built-in templates cannot be deleted",
        )

    await asyncio.to_thread(
        database.delete_template,
        template_id,
    )

    return Response(status_code=204)

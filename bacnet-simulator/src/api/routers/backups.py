from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, Response

from ...bacnet import backup


router = APIRouter(
    prefix="/backups",
    tags=["backups"],
)


def get_engine(request: Request):
    engine = getattr(
        request.app.state,
        "engine",
        None,
    )

    if engine is None:
        raise HTTPException(
            status_code=503,
            detail="Simulation engine is unavailable",
        )

    return engine


@router.get("")
async def list_backups():
    return await asyncio.to_thread(
        backup.list_backups
    )


@router.post("", status_code=201)
async def create_backup():
    try:
        return await asyncio.to_thread(
            backup.create_backup
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=str(exc),
        ) from exc


@router.post("/{file_name}/restore")
async def restore_backup(
    file_name: str,
    request: Request,
):
    try:
        result = await asyncio.to_thread(
            backup.restore_backup,
            file_name,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc

    engine = get_engine(request)

    asyncio.create_task(engine.reload())
    engine.reset()

    return result


@router.delete(
    "/{file_name}",
    status_code=204,
)
async def delete_backup(
    file_name: str,
) -> Response:
    deleted = await asyncio.to_thread(
        backup.delete_backup,
        file_name,
    )

    if not deleted:
        raise HTTPException(
            status_code=404,
            detail="Backup not found",
        )

    return Response(status_code=204)


@router.get("/{file_name}/download")
async def download_backup(
    file_name: str,
):
    safe_name = Path(file_name).name
    path = backup.get_backup_dir() / safe_name

    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail="Backup not found",
        )

    return FileResponse(
        str(path),
        media_type="application/octet-stream",
        filename=path.name,
    )


@router.post(
    "/upload",
    status_code=201,
)
async def upload_backup(
    file: UploadFile = File(...),
):
    data = await file.read()

    if not data:
        raise HTTPException(
            status_code=400,
            detail="Uploaded file is empty",
        )

    try:
        return await asyncio.to_thread(
            backup.save_uploaded_backup,
            file.filename or "backup.db",
            data,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=str(exc),
        ) from exc
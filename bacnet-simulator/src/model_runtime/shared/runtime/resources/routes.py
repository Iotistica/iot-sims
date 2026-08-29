from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from ..errors import http_error
from ..storage import RUNTIME_DATA_DIR
from .manager import ResourceManager
from .models import ResourceResponse, resource_to_response

router = APIRouter()

resource_manager = ResourceManager(storage_root=RUNTIME_DATA_DIR / "resources")


@router.post("/resources", response_model=ResourceResponse, status_code=201)
async def upload_resource(file: UploadFile = File(...)):
    try:
        resource = await resource_manager.upload(file)
        return resource_to_response(resource)
    except Exception as exc:
        raise http_error(exc)


@router.get("/resources")
def list_resources():
    return {"resources": [resource_to_response(r) for r in resource_manager.list()]}


@router.get("/resources/{filename}/content")
def download_resource_content(filename: str):
    # Generic byte-fetch for an already-uploaded resource -- not
    # weather-specific. Lets a caller (e.g. bacnet-simulator's weather
    # provenance parsing) re-read a file it uploaded earlier without
    # keeping its own copy, the same way it never needs to keep its own
    # copy of anything else staged here.
    resource = resource_manager.get(filename)
    if resource is None:
        raise HTTPException(status_code=404, detail=f"Resource {filename!r} not found")
    return FileResponse(resource.path, filename=resource.filename)

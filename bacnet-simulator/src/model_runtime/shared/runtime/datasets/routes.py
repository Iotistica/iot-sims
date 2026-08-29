from __future__ import annotations

from fastapi import APIRouter, File, UploadFile

from ..errors import http_error
from ..state import catalog
from ..storage import RUNTIME_DATA_DIR
from .manager import DatasetManager
from .models import DatasetResponse, dataset_to_response

router = APIRouter()

dataset_manager = DatasetManager(catalog=catalog, storage_root=RUNTIME_DATA_DIR)


@router.post("/models/{model_id}/datasets", response_model=DatasetResponse, status_code=201)
async def upload_dataset(model_id: str, file: UploadFile = File(...)):
    try:
        dataset = await dataset_manager.upload(model_id, file)
        return dataset_to_response(dataset)
    except Exception as exc:
        raise http_error(exc)


@router.get("/models/{model_id}/datasets")
def list_datasets(model_id: str):
    try:
        catalog.get(model_id)
        return {"datasets": [dataset_to_response(d) for d in dataset_manager.list(model_id)]}
    except Exception as exc:
        raise http_error(exc)


@router.get("/models/{model_id}/datasets/{dataset_id}", response_model=DatasetResponse)
def get_dataset(model_id: str, dataset_id: str):
    try:
        return dataset_to_response(dataset_manager.get(model_id, dataset_id))
    except Exception as exc:
        raise http_error(exc)


@router.delete("/models/{model_id}/datasets/{dataset_id}", status_code=204)
def delete_dataset(model_id: str, dataset_id: str):
    try:
        dataset_manager.delete(model_id, dataset_id)
    except Exception as exc:
        raise http_error(exc)

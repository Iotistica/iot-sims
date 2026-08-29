from __future__ import annotations

from fastapi import APIRouter

from ..errors import http_error
from ..state import catalog
from ..storage import RUNTIME_DATA_DIR
from ..datasets.routes import dataset_manager
from .manager import CalibrationManager
from .models import CalibrationJobResponse, CreateCalibrationRequest, job_to_response
from .runners import HEBOCalibrationRunner

router = APIRouter()

calibration_manager = CalibrationManager(
    catalog=catalog,
    dataset_manager=dataset_manager,
    storage_root=RUNTIME_DATA_DIR,
    runners=[HEBOCalibrationRunner(catalog=catalog, dataset_manager=dataset_manager, storage_root=RUNTIME_DATA_DIR)],
)


@router.post("/models/{model_id}/calibrations", response_model=CalibrationJobResponse, status_code=201)
def create_calibration(model_id: str, request: CreateCalibrationRequest):
    try:
        job = calibration_manager.create_job(
            model_id=model_id,
            method=request.method,
            dataset_id=request.dataset_id,
            configuration=request.configuration,
            experiment_id=request.experiment_id,
        )
        return job_to_response(job)
    except Exception as exc:
        raise http_error(exc)


@router.get("/models/{model_id}/calibrations")
def list_calibrations(model_id: str):
    try:
        catalog.get(model_id)
        return {"jobs": [job_to_response(j) for j in calibration_manager.list_jobs(model_id)]}
    except Exception as exc:
        raise http_error(exc)


@router.get("/models/{model_id}/calibrations/{job_id}", response_model=CalibrationJobResponse)
def get_calibration(model_id: str, job_id: str):
    try:
        return job_to_response(calibration_manager.get_job(model_id, job_id))
    except Exception as exc:
        raise http_error(exc)


@router.get("/models/{model_id}/calibrations/{job_id}/results")
def get_calibration_results(model_id: str, job_id: str):
    try:
        return calibration_manager.get_result(model_id, job_id)
    except Exception as exc:
        raise http_error(exc)


@router.post("/models/{model_id}/calibrations/{job_id}/cancel", response_model=CalibrationJobResponse)
def cancel_calibration(model_id: str, job_id: str):
    try:
        job = calibration_manager.cancel_job(model_id, job_id)
        return job_to_response(job)
    except Exception as exc:
        raise http_error(exc)

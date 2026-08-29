from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def calibration_job_dir(storage_root: Path, model_id: str, job_id: str) -> Path:
    """<storage_root>/<model_id>/jobs/<job_id>/ -- the one place this layout
    convention is defined; CalibrationManager and every CalibrationRunner
    import it from here rather than each hardcoding the path shape."""
    return storage_root / model_id / "jobs" / job_id


class CalibrationStatus(str, Enum):
    QUEUED = "QUEUED"
    VALIDATING = "VALIDATING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset(
    {CalibrationStatus.COMPLETED, CalibrationStatus.FAILED, CalibrationStatus.CANCELLED}
)


@dataclass
class CalibrationJob:
    id: str
    model_id: str
    method: str
    dataset_id: str
    configuration: dict[str, Any]
    status: CalibrationStatus

    created_at: float
    started_at: float | None = None
    completed_at: float | None = None

    result_path: str | None = None
    error: str | None = None
    experiment_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "model_id": self.model_id,
            "method": self.method,
            "dataset_id": self.dataset_id,
            "configuration": self.configuration,
            "status": self.status.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result_path": self.result_path,
            "error": self.error,
            "experiment_id": self.experiment_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CalibrationJob":
        return cls(
            id=data["id"],
            model_id=data["model_id"],
            method=data["method"],
            dataset_id=data["dataset_id"],
            configuration=dict(data.get("configuration") or {}),
            status=CalibrationStatus(data["status"]),
            created_at=data["created_at"],
            started_at=data.get("started_at"),
            completed_at=data.get("completed_at"),
            result_path=data.get("result_path"),
            error=data.get("error"),
            experiment_id=data.get("experiment_id"),
        )


class CreateCalibrationRequest(BaseModel):
    method: str
    dataset_id: str
    configuration: dict[str, Any] = Field(default_factory=dict)
    experiment_id: str | None = None


class CalibrationJobResponse(BaseModel):
    job_id: str
    model_id: str
    method: str
    dataset_id: str
    status: str
    experiment_id: str | None = None
    created_at: float
    started_at: float | None = None
    completed_at: float | None = None
    error: str | None = None


def job_to_response(job: CalibrationJob) -> CalibrationJobResponse:
    return CalibrationJobResponse(
        job_id=job.id,
        model_id=job.model_id,
        method=job.method,
        dataset_id=job.dataset_id,
        status=job.status.value,
        experiment_id=job.experiment_id,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error=job.error,
    )

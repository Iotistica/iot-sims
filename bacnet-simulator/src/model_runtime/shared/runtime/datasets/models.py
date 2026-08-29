from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import BaseModel


class DatasetStatus(str, Enum):
    READY = "READY"
    # UPLOADING/VALIDATING/FAILED are reserved for a later version that scans
    # large files asynchronously or wants to keep a record of a failed
    # upload. V1 only ever produces READY: a failed upload is cleaned up
    # (temp file removed) and returns an HTTP error instead of persisting a
    # Dataset at all -- see DatasetManager.upload.


@dataclass
class Dataset:
    id: str
    model_id: str
    filename: str
    size_bytes: int
    created_at: float
    status: DatasetStatus = DatasetStatus.READY
    columns: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.id,
            "model_id": self.model_id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "created_at": self.created_at,
            "status": self.status.value,
            "columns": self.columns,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Dataset":
        return cls(
            id=data["dataset_id"],
            model_id=data["model_id"],
            filename=data["filename"],
            size_bytes=data["size_bytes"],
            created_at=data["created_at"],
            status=DatasetStatus(data.get("status", DatasetStatus.READY.value)),
            columns=list(data.get("columns") or []),
        )


class DatasetResponse(BaseModel):
    dataset_id: str
    model_id: str
    filename: str
    size_bytes: int
    created_at: float
    status: str
    columns: list[str] = []


def dataset_to_response(dataset: Dataset) -> DatasetResponse:
    return DatasetResponse(**dataset.to_dict())

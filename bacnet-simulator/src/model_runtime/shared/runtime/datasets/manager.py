from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import UploadFile

from ..models.catalog import ModelCatalog
from ..errors import ConflictError
from .models import Dataset, DatasetStatus

_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024


class DatasetManager:
    """Generic runtime datasets, independent of calibration. A dataset is
    uploaded once and referenced by dataset_id from any number of downstream
    consumers (today: calibration jobs; later: Morris sensitivity,
    validation, replay). See shared/runtime/datasets/models.py for the
    Dataset shape and the plan this implements
    (C:\\Users\\Dan\\.claude\\plans\\linked-cooking-sloth.md)."""

    def __init__(self, catalog: ModelCatalog, storage_root: Path) -> None:
        self._catalog = catalog
        self._storage_root = storage_root

    def _model_dir(self, model_id: str) -> Path:
        return self._storage_root / model_id / "datasets"

    def _dataset_dir(self, model_id: str, dataset_id: str) -> Path:
        return self._model_dir(model_id) / dataset_id

    def _data_path(self, model_id: str, dataset_id: str, filename: str) -> Path:
        suffix = Path(filename).suffix
        return self._dataset_dir(model_id, dataset_id) / f"data{suffix}"

    async def upload(self, model_id: str, file: UploadFile) -> Dataset:
        self._catalog.get(model_id)  # KeyError -> 404 if unknown model

        model_dir = self._model_dir(model_id)
        model_dir.mkdir(parents=True, exist_ok=True)

        dataset_id = f"ds_{uuid.uuid4().hex[:8]}"
        filename = file.filename or "data"
        suffix = Path(filename).suffix
        tmp_path = model_dir / f".tmp-{dataset_id}{suffix}"

        size_bytes = 0
        try:
            with tmp_path.open("wb") as out:
                while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                    out.write(chunk)
                    size_bytes += len(chunk)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        # Only now, with the file fully and correctly on disk, does it become
        # a real dataset directory/id -- a crash or exception above never
        # leaves a half-uploaded dataset visible via list()/get().
        dataset_dir = self._dataset_dir(model_id, dataset_id)
        dataset_dir.mkdir(parents=True, exist_ok=True)
        final_path = dataset_dir / f"data{suffix}"
        try:
            tmp_path.replace(final_path)
        except Exception:
            shutil.rmtree(dataset_dir, ignore_errors=True)
            tmp_path.unlink(missing_ok=True)
            raise

        columns = self._peek_columns(final_path)

        dataset = Dataset(
            id=dataset_id,
            model_id=model_id,
            filename=filename,
            size_bytes=size_bytes,
            created_at=time.time(),
            status=DatasetStatus.READY,
            columns=columns,
        )
        (dataset_dir / "metadata.json").write_text(
            json.dumps(dataset.to_dict(), indent=2), encoding="utf-8"
        )
        return dataset

    @staticmethod
    def _peek_columns(path: Path) -> list[str]:
        """Cheap best-effort header read for CSV-like files -- a single
        readline(), never a full scan of a potentially huge dataset."""
        if path.suffix.lower() != ".csv":
            return []
        try:
            with path.open("r", encoding="utf-8", errors="replace") as f:
                header = f.readline()
        except OSError:
            return []
        return [c.strip() for c in header.strip().split(",") if c.strip()]

    def get(self, model_id: str, dataset_id: str) -> Dataset:
        metadata_path = self._dataset_dir(model_id, dataset_id) / "metadata.json"
        if not metadata_path.exists():
            raise KeyError(f"Unknown dataset: {dataset_id}")
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
        if data.get("model_id") != model_id:
            raise KeyError(f"Unknown dataset: {dataset_id}")
        return Dataset.from_dict(data)

    def list(self, model_id: str) -> list[Dataset]:
        model_dir = self._model_dir(model_id)
        if not model_dir.exists():
            return []
        datasets = []
        for metadata_path in sorted(model_dir.glob("*/metadata.json")):
            try:
                datasets.append(Dataset.from_dict(json.loads(metadata_path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return datasets

    def resolve_path(self, model_id: str, dataset_id: str) -> Path:
        """The only way calibration (or any future consumer) code obtains a
        filesystem path for a dataset -- always through a validated
        dataset_id, never a raw path from the API layer."""
        dataset = self.get(model_id, dataset_id)
        return self._data_path(model_id, dataset_id, dataset.filename)

    def _referencing_job_ids(self, model_id: str, dataset_id: str) -> list[str]:
        jobs_root = self._storage_root / model_id / "jobs"
        if not jobs_root.exists():
            return []
        referencing: list[str] = []
        for job_path in jobs_root.glob("*/job.json"):
            try:
                job_data = json.loads(job_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job_data.get("dataset_id") == dataset_id:
                referencing.append(job_data.get("id", job_path.parent.name))
        return referencing

    def delete(self, model_id: str, dataset_id: str) -> None:
        self.get(model_id, dataset_id)  # KeyError -> 404 if missing/wrong model
        if self._referencing_job_ids(model_id, dataset_id):
            raise ConflictError("Dataset is referenced by one or more calibration jobs.")
        shutil.rmtree(self._dataset_dir(model_id, dataset_id), ignore_errors=True)

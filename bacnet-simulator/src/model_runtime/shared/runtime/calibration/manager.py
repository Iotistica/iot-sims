from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from ..models.catalog import ModelCatalog
from ..datasets.manager import DatasetManager
from ..errors import ConflictError
from .models import CalibrationJob, CalibrationStatus, TERMINAL_STATUSES, calibration_job_dir
from .runners import CalibrationRunner

logger = logging.getLogger(__name__)

_STALE_RESTART_MESSAGE = "Runtime restarted while calibration was running."


class CalibrationManager:
    """Generic, method-agnostic calibration job lifecycle. Knows nothing
    HEBO-specific -- dispatches to whichever CalibrationRunner is registered
    for job.method and persists exactly whatever dict that runner's run()
    returns. See the plan
    (C:\\Users\\Dan\\.claude\\plans\\linked-cooking-sloth.md) for the full
    design and the ownership-boundary rationale."""

    def __init__(
        self,
        catalog: ModelCatalog,
        dataset_manager: DatasetManager,
        storage_root: Path,
        runners: Iterable[CalibrationRunner],
    ) -> None:
        self._catalog = catalog
        self._dataset_manager = dataset_manager
        self._storage_root = storage_root
        self._runners: dict[str, CalibrationRunner] = {r.method: r for r in runners}
        self._jobs: dict[str, CalibrationJob] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._lock = threading.Lock()
        self._load_persisted_jobs()
        self._reconcile_stale_jobs()

    # -- persistence ---------------------------------------------------

    def _job_dir(self, model_id: str, job_id: str) -> Path:
        return calibration_job_dir(self._storage_root, model_id, job_id)

    def _persist(self, job: CalibrationJob) -> None:
        job_dir = self._job_dir(job.model_id, job.id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "job.json").write_text(json.dumps(job.to_dict(), indent=2), encoding="utf-8")

    def _load_persisted_jobs(self) -> None:
        for job_path in sorted(self._storage_root.glob("*/jobs/*/job.json")):
            try:
                job = CalibrationJob.from_dict(json.loads(job_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, KeyError, ValueError):
                logger.warning("Skipping unreadable calibration job at %s", job_path)
                continue
            self._jobs[job.id] = job

    def _reconcile_stale_jobs(self) -> None:
        """A job left QUEUED/VALIDATING/RUNNING in a previous runtime process
        has an orphaned (or nonexistent) subprocess -- no attempt to resume
        it, just mark it FAILED so it doesn't appear stuck RUNNING forever."""
        for job in self._jobs.values():
            if job.status not in TERMINAL_STATUSES:
                job.status = CalibrationStatus.FAILED
                job.error = _STALE_RESTART_MESSAGE
                job.completed_at = time.time()
                self._persist(job)

    # -- job lifecycle ---------------------------------------------------

    def create_job(
        self,
        model_id: str,
        method: str,
        dataset_id: str,
        configuration: dict[str, Any] | None = None,
        experiment_id: str | None = None,
    ) -> CalibrationJob:
        configuration = dict(configuration or {})

        self._catalog.get(model_id)  # KeyError -> 404 if unknown
        self._dataset_manager.get(model_id, dataset_id)  # KeyError -> 404 if unknown/wrong model

        runner = self._runners.get(method)
        if runner is None:
            raise ValueError(
                f"Unsupported calibration method: {method!r}. "
                f"Available: {sorted(self._runners)}"
            )
        runner.validate_configuration(configuration)

        job = CalibrationJob(
            id=f"cal_{uuid.uuid4().hex[:10]}",
            model_id=model_id,
            method=method,
            dataset_id=dataset_id,
            configuration=configuration,
            status=CalibrationStatus.QUEUED,
            created_at=time.time(),
            experiment_id=experiment_id,
        )
        job_dir = self._job_dir(model_id, job.id)
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "request.json").write_text(
            json.dumps(
                {
                    "method": method,
                    "dataset_id": dataset_id,
                    "configuration": configuration,
                    "experiment_id": experiment_id,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        with self._lock:
            self._jobs[job.id] = job
            self._cancel_events[job.id] = threading.Event()
        self._persist(job)

        threading.Thread(target=self._execute, args=(job, runner), daemon=True).start()
        return job

    def _execute(self, job: CalibrationJob, runner: CalibrationRunner) -> None:
        cancel_event = self._cancel_events[job.id]

        job.status = CalibrationStatus.VALIDATING
        self._persist(job)

        job.status = CalibrationStatus.RUNNING
        job.started_at = time.time()
        self._persist(job)

        try:
            result = runner.run(job)
        except Exception as exc:
            job.completed_at = time.time()
            if cancel_event.is_set():
                job.status = CalibrationStatus.CANCELLED
                job.error = "Cancelled by request."
            else:
                job.status = CalibrationStatus.FAILED
                job.error = str(exc)
                logger.exception("Calibration job %s failed", job.id)
            self._persist(job)
            return

        job.completed_at = time.time()
        if cancel_event.is_set():
            job.status = CalibrationStatus.CANCELLED
            job.error = "Cancelled by request."
            self._persist(job)
            return

        job_dir = self._job_dir(job.model_id, job.id)
        result_path = job_dir / "result.json"
        result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
        job.status = CalibrationStatus.COMPLETED
        job.result_path = str(result_path)
        self._persist(job)

    # -- queries ---------------------------------------------------

    def get_job(self, model_id: str, job_id: str) -> CalibrationJob:
        job = self._jobs.get(job_id)
        if job is None or job.model_id != model_id:
            raise KeyError(f"Unknown calibration job: {job_id}")
        return job

    def list_jobs(self, model_id: str) -> list[CalibrationJob]:
        return sorted(
            (j for j in self._jobs.values() if j.model_id == model_id),
            key=lambda j: j.created_at,
            reverse=True,
        )

    def get_result(self, model_id: str, job_id: str) -> dict[str, Any]:
        job = self.get_job(model_id, job_id)
        if job.status != CalibrationStatus.COMPLETED:
            raise ConflictError(f"Calibration job is {job.status.value}, not yet completed.")
        if not job.result_path:
            raise KeyError(f"No result available for calibration job: {job_id}")
        return json.loads(Path(job.result_path).read_text(encoding="utf-8"))

    def cancel_job(self, model_id: str, job_id: str) -> CalibrationJob:
        job = self.get_job(model_id, job_id)
        if job.status in TERMINAL_STATUSES:
            raise ConflictError(f"Calibration job already finished with status {job.status.value}.")
        self._cancel_events[job_id].set()
        runner = self._runners[job.method]
        runner.cancel(job)
        return job

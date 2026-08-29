from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Protocol

from ..datasets.manager import DatasetManager
from ..models.catalog import ModelCatalog
from .models import CalibrationJob, calibration_job_dir

REPO_ROOT = Path(__file__).resolve().parents[3]
HEBO_SCRIPT = REPO_ROOT / "shared" / "run_hebo_calibration.py"


class CalibrationRunner(Protocol):
    method: str

    def validate_configuration(self, configuration: dict[str, Any]) -> None:
        """Raise ValueError on any unknown key or bad value. Called
        synchronously from CalibrationManager.create_job so bad input gets a
        fast 400 instead of failing later in the background thread."""
        ...

    def run(self, job: CalibrationJob) -> dict[str, Any]:
        """Blocking. Returns the common result contract dict (see the plan's
        "Common result contract" section) -- already normalized;
        CalibrationManager treats the return value as opaque and just
        persists it. Raises on failure; the message becomes job.error."""
        ...

    def cancel(self, job: CalibrationJob) -> None:
        """Best-effort: terminate the live subprocess for this job, if any.
        Fire-and-forget -- does not itself change job.status."""
        ...


# key -> (CLI flag, coercer). skip_baseline is handled separately since it's
# a store_true flag with no value.
_HEBO_CONFIG_SPEC: dict[str, tuple[str, type]] = {
    "iterations": ("--iterations", int),
    "batch_size": ("--batch-size", int),
    "seed": ("--seed", int),
    "timeout": ("--timeout", float),
    "failure_penalty": ("--failure-penalty", float),
    "fmi_type": ("--fmi-type", str),
    "solver": ("--solver", str),
}
_HEBO_BOOLEAN_FLAGS: dict[str, str] = {
    "skip_baseline": "--skip-baseline",
}
_HEBO_FMI_TYPES = {"ModelExchange", "CoSimulation"}


class HEBOCalibrationRunner:
    method = "hebo"

    def __init__(self, catalog: ModelCatalog, dataset_manager: DatasetManager, storage_root: Path) -> None:
        self._catalog = catalog
        self._dataset_manager = dataset_manager
        self._storage_root = storage_root
        self._processes: dict[str, subprocess.Popen] = {}

    def validate_configuration(self, configuration: dict[str, Any]) -> None:
        allowed = set(_HEBO_CONFIG_SPEC) | set(_HEBO_BOOLEAN_FLAGS)
        unknown = set(configuration) - allowed
        if unknown:
            raise ValueError(f"Unsupported HEBO configuration key(s): {sorted(unknown)}")

        for key, (_, coercer) in _HEBO_CONFIG_SPEC.items():
            if key not in configuration:
                continue
            value = configuration[key]
            try:
                coerced = coercer(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"HEBO configuration {key!r} must be a {coercer.__name__}") from exc
            if key in ("iterations", "batch_size") and coerced <= 0:
                raise ValueError(f"HEBO configuration {key!r} must be positive")
            if key == "fmi_type" and coerced not in _HEBO_FMI_TYPES:
                raise ValueError(f"HEBO configuration 'fmi_type' must be one of {sorted(_HEBO_FMI_TYPES)}")

        for key in _HEBO_BOOLEAN_FLAGS:
            if key in configuration and not isinstance(configuration[key], bool):
                raise ValueError(f"HEBO configuration {key!r} must be a boolean")

    def _build_command(self, job: CalibrationJob, job_dir: Path, fmu_path: Path, metadata_path: Path, dataset_path: Path) -> list[str]:
        cmd: list[str] = [
            sys.executable, str(HEBO_SCRIPT),
            "--fmu", str(fmu_path),
            "--metadata", str(metadata_path),
            "--data", str(dataset_path),
            "--output", str(job_dir / "raw_result.json"),
        ]
        for key, (flag, coercer) in _HEBO_CONFIG_SPEC.items():
            if key in job.configuration:
                cmd += [flag, str(coercer(job.configuration[key]))]
        for key, flag in _HEBO_BOOLEAN_FLAGS.items():
            if job.configuration.get(key):
                cmd.append(flag)
        return cmd

    def run(self, job: CalibrationJob) -> dict[str, Any]:
        model = self._catalog.get(job.model_id)
        fmu_path = model.fmu_path
        metadata_path = model.fmu_path.parent / "model.json"
        dataset_path = self._dataset_manager.resolve_path(job.model_id, job.dataset_id)

        job_dir = calibration_job_dir(self._storage_root, job.model_id, job.id)
        job_dir.mkdir(parents=True, exist_ok=True)

        cmd = self._build_command(job, job_dir, fmu_path, metadata_path, dataset_path)

        # Deprioritize the calibration process's CPU scheduling (not memory, not a hard cap) so a
        # HEBO calibration burst yields to co-located, latency-sensitive processes (e.g. the BACnet
        # tick loop) under contention, when this container is deployed alongside them. `nice`
        # execs the target command in place (no fork), so `proc.pid` below still refers to the
        # actual calibration script and `cancel()`'s `proc.terminate()` is unaffected.
        cmd = ["nice", "-n", "10"] + cmd

        stdout_path = job_dir / "stdout.log"
        stderr_path = job_dir / "stderr.log"
        start = time.time()
        with stdout_path.open("wb") as stdout_f, stderr_path.open("wb") as stderr_f:
            proc = subprocess.Popen(cmd, stdout=stdout_f, stderr=stderr_f, cwd=str(REPO_ROOT))
            self._processes[job.id] = proc
            try:
                returncode = proc.wait()
            finally:
                self._processes.pop(job.id, None)
        duration_seconds = time.time() - start

        if returncode != 0:
            tail = _tail(stderr_path)
            raise RuntimeError(f"HEBO calibration exited with code {returncode}: {tail}")

        raw_result_path = job_dir / "raw_result.json"
        raw = _read_json(raw_result_path)
        return self._normalize(job, raw, duration_seconds)

    def cancel(self, job: CalibrationJob) -> None:
        proc = self._processes.get(job.id)
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _normalize(self, job: CalibrationJob, raw: dict[str, Any], duration_seconds: float) -> dict[str, Any]:
        goal = raw.get("goal", {})
        baseline = raw.get("baseline") or {}
        best = raw.get("best", {})
        failures = raw.get("failures", {})
        optimizer = raw.get("optimizer", {})
        return {
            "job_id": job.id,
            "experiment_id": job.experiment_id,
            "model_id": job.model_id,
            "method": job.method,
            "dataset_id": job.dataset_id,
            "objective": {
                "metric": goal.get("metric"),
                "baseline": baseline.get("objective"),
                "best": best.get("objective"),
                "improvement_pct": best.get("improvement_pct"),
            },
            "best_parameters": best.get("parameters", {}),
            "execution": {
                "evaluations": optimizer.get("evaluations_completed"),
                "failed_evaluations": failures.get("count", 0),
                "duration_seconds": duration_seconds,
            },
            "method_result": raw,
        }


def _tail(path: Path, max_chars: int = 2000) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return text[-max_chars:]


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))

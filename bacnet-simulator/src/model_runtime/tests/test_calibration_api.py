import json
import threading
import time
from pathlib import Path

from fastapi.testclient import TestClient

from shared.runtime.app import app
from shared.runtime.calibration import routes as calibration_routes
from shared.runtime.calibration import runners as calibration_runners
from shared.runtime.datasets import routes as dataset_routes


def _model_id(client: TestClient, slug: str) -> str:
    models = client.get("/models").json()["models"]
    return next(item["id"] for item in models if item["slug"] == slug)


def _isolate(monkeypatch, tmp_path):
    """Points the dataset manager, calibration manager, and the HEBO
    runner's own storage_root at a fresh tmp dir, and clears any in-memory
    jobs left over from another test/import-time state -- so every test
    starts from a clean slate regardless of the real (gitignored)
    RUNTIME_DATA_DIR."""
    monkeypatch.setattr(dataset_routes.dataset_manager, "_storage_root", tmp_path)
    monkeypatch.setattr(calibration_routes.calibration_manager, "_storage_root", tmp_path)
    monkeypatch.setattr(calibration_routes.calibration_manager, "_jobs", {})
    monkeypatch.setattr(calibration_routes.calibration_manager, "_cancel_events", {})
    for runner in calibration_routes.calibration_manager._runners.values():
        monkeypatch.setattr(runner, "_storage_root", tmp_path)
    return calibration_routes.calibration_manager


_CANNED_RAW_RESULT = {
    "goal": {"metadata_output": "supply_air_temp_c", "fmu_variable": "TSup", "metric": "cv_rmse"},
    "optimizer": {
        "method": "hebo", "iterations_requested": 5, "evaluations_completed": 5,
        "batch_size": 1, "seed": 42, "failure_penalty": 1e6,
        "fmi_type": "CoSimulation", "solver": "CVode",
    },
    "baseline": {"parameters": {}, "objective": 0.2, "metrics": {}, "status": "ok"},
    "best": {
        "objective": 0.1, "parameters": {"UA_multiplier": 1.2}, "metrics": {},
        "improvement": 0.1, "improvement_pct": 50.0,
    },
    "failures": {"count": 0, "rate_pct": 0.0, "records": []},
}


def _output_path(cmd: list[str]) -> Path:
    return Path(cmd[cmd.index("--output") + 1])


class _FakePopen:
    """Finishes immediately with exit code 0, writing a canned HEBO result
    to --output -- stands in for the real subprocess, which needs fmpy/HEBO
    installed and isn't available in the test environment."""

    def __init__(self, cmd, stdout=None, stderr=None, cwd=None):
        _output_path(cmd).write_text(json.dumps(_CANNED_RAW_RESULT), encoding="utf-8")
        self.returncode = 0

    def wait(self):
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        pass


class _FakeHangingPopen:
    """Never exits on its own -- blocks in wait() until terminate() is
    called, then exits nonzero (simulating SIGTERM), for the cancel test."""

    def __init__(self, cmd, stdout=None, stderr=None, cwd=None):
        self._terminated = threading.Event()
        self.returncode = None

    def wait(self):
        self._terminated.wait(timeout=5)
        self.returncode = -15
        return self.returncode

    def poll(self):
        return self.returncode

    def terminate(self):
        self._terminated.set()


def _upload_dataset(client: TestClient, model_id: str) -> str:
    resp = client.post(
        f"/models/{model_id}/datasets",
        files={"file": ("history.csv", b"timestamp,zone_temp_c\n2024-01-01T00:00:00,21.5\n", "text/csv")},
    )
    assert resp.status_code == 201
    return resp.json()["dataset_id"]


def _poll_status(client: TestClient, model_id: str, job_id: str, want: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = client.get(f"/models/{model_id}/calibrations/{job_id}")
        assert resp.status_code == 200
        body = resp.json()
        if body["status"] == want:
            return body
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} never reached {want!r} (last status: {body['status']!r})")


def test_create_poll_and_fetch_results(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(calibration_runners.subprocess, "Popen", _FakePopen)
    client = TestClient(app)
    model_id = _model_id(client, "SimpleVAVZone")
    dataset_id = _upload_dataset(client, model_id)

    created = client.post(
        f"/models/{model_id}/calibrations",
        json={"method": "hebo", "dataset_id": dataset_id, "configuration": {"iterations": 5}, "experiment_id": "exp_1"},
    )
    assert created.status_code == 201
    job = created.json()
    # The response reflects live, shared job state -- with an
    # instant-completing fake subprocess, the background thread can race
    # ahead of this assertion, so any non-terminal-or-already-done status is
    # valid here; the poll below is the real completion check.
    assert job["status"] in {"QUEUED", "VALIDATING", "RUNNING", "COMPLETED"}
    assert job["dataset_id"] == dataset_id

    _poll_status(client, model_id, job["job_id"], "COMPLETED")

    results = client.get(f"/models/{model_id}/calibrations/{job['job_id']}/results")
    assert results.status_code == 200
    body = results.json()
    assert body["dataset_id"] == dataset_id
    assert body["objective"]["best"] == 0.1
    assert body["best_parameters"] == {"UA_multiplier": 1.2}
    assert body["execution"]["evaluations"] == 5
    assert body["method_result"] == _CANNED_RAW_RESULT

    listed = client.get(f"/models/{model_id}/calibrations")
    assert job["job_id"] in [j["job_id"] for j in listed.json()["jobs"]]


def test_same_dataset_reused_by_two_jobs(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(calibration_runners.subprocess, "Popen", _FakePopen)
    client = TestClient(app)
    model_id = _model_id(client, "SimpleVAVZone")
    dataset_id = _upload_dataset(client, model_id)

    job_a = client.post(
        f"/models/{model_id}/calibrations",
        json={"method": "hebo", "dataset_id": dataset_id},
    ).json()
    job_b = client.post(
        f"/models/{model_id}/calibrations",
        json={"method": "hebo", "dataset_id": dataset_id},
    ).json()
    assert job_a["job_id"] != job_b["job_id"]

    _poll_status(client, model_id, job_a["job_id"], "COMPLETED")
    _poll_status(client, model_id, job_b["job_id"], "COMPLETED")

    dataset_dirs = list((tmp_path / model_id / "datasets").glob("ds_*"))
    assert len(dataset_dirs) == 1


def test_validation_errors(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(calibration_runners.subprocess, "Popen", _FakePopen)
    client = TestClient(app)
    model_id = _model_id(client, "SimpleVAVZone")
    dataset_id = _upload_dataset(client, model_id)

    unknown_model = client.post(
        "/models/does-not-exist/calibrations",
        json={"method": "hebo", "dataset_id": dataset_id},
    )
    assert unknown_model.status_code == 404

    unknown_dataset = client.post(
        f"/models/{model_id}/calibrations",
        json={"method": "hebo", "dataset_id": "ds_missing"},
    )
    assert unknown_dataset.status_code == 404

    unsupported_method = client.post(
        f"/models/{model_id}/calibrations",
        json={"method": "differential_evolution", "dataset_id": dataset_id},
    )
    assert unsupported_method.status_code == 400

    bad_config = client.post(
        f"/models/{model_id}/calibrations",
        json={"method": "hebo", "dataset_id": dataset_id, "configuration": {"bogus_flag": 1}},
    )
    assert bad_config.status_code == 400

    # A hanging fake process, so the job is guaranteed to still be RUNNING
    # (not racing a real fast-completing fake) when /results is checked.
    monkeypatch.setattr(calibration_runners.subprocess, "Popen", _FakeHangingPopen)
    job = client.post(
        f"/models/{model_id}/calibrations",
        json={"method": "hebo", "dataset_id": dataset_id},
    ).json()
    _poll_status(client, model_id, job["job_id"], "RUNNING")
    too_early = client.get(f"/models/{model_id}/calibrations/{job['job_id']}/results")
    assert too_early.status_code == 409


def test_cancel(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    monkeypatch.setattr(calibration_runners.subprocess, "Popen", _FakeHangingPopen)
    client = TestClient(app)
    model_id = _model_id(client, "SimpleVAVZone")
    dataset_id = _upload_dataset(client, model_id)

    job = client.post(
        f"/models/{model_id}/calibrations",
        json={"method": "hebo", "dataset_id": dataset_id},
    ).json()
    _poll_status(client, model_id, job["job_id"], "RUNNING")

    cancelled = client.post(f"/models/{model_id}/calibrations/{job['job_id']}/cancel")
    assert cancelled.status_code == 200

    _poll_status(client, model_id, job["job_id"], "CANCELLED")

    already_terminal = client.post(f"/models/{model_id}/calibrations/{job['job_id']}/cancel")
    assert already_terminal.status_code == 409


def test_startup_reconciles_stale_running_jobs(tmp_path):
    from shared.runtime.calibration.manager import CalibrationManager
    from shared.runtime.datasets.manager import DatasetManager
    from shared.runtime.state import catalog

    model_id = next(m["id"] for m in catalog.list_models() if m["slug"] == "SimpleVAVZone")
    job_dir = tmp_path / model_id / "jobs" / "cal_stale0001"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(
        json.dumps({
            "id": "cal_stale0001", "model_id": model_id, "method": "hebo",
            "dataset_id": "ds_whatever", "configuration": {}, "status": "RUNNING",
            "created_at": time.time(), "started_at": time.time(),
            "completed_at": None, "result_path": None, "error": None, "experiment_id": None,
        }),
        encoding="utf-8",
    )

    dataset_manager = DatasetManager(catalog=catalog, storage_root=tmp_path)
    manager = CalibrationManager(catalog=catalog, dataset_manager=dataset_manager, storage_root=tmp_path, runners=[])

    job = manager.get_job(model_id, "cal_stale0001")
    assert job.status.value == "FAILED"
    assert "restarted" in job.error.lower()

from fastapi.testclient import TestClient

from shared.runtime.app import app
from shared.runtime.datasets import routes as dataset_routes


def _model_id(client: TestClient, slug: str) -> str:
    models = client.get("/models").json()["models"]
    return next(item["id"] for item in models if item["slug"] == slug)


def _isolate(monkeypatch, tmp_path):
    """Points the module-level DatasetManager singleton at a fresh tmp dir
    for this test, instead of the real (gitignored) RUNTIME_DATA_DIR."""
    monkeypatch.setattr(dataset_routes.dataset_manager, "_storage_root", tmp_path)
    return dataset_routes.dataset_manager


def test_upload_list_get_delete(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    client = TestClient(app)
    model_id = _model_id(client, "SimpleVAVZone")

    uploaded = client.post(
        f"/models/{model_id}/datasets",
        files={"file": ("history.csv", b"timestamp,zone_temp_c\n2024-01-01T00:00:00,21.5\n", "text/csv")},
    )
    assert uploaded.status_code == 201
    body = uploaded.json()
    assert body["dataset_id"].startswith("ds_")
    assert body["status"] == "READY"
    assert body["filename"] == "history.csv"
    assert body["columns"] == ["timestamp", "zone_temp_c"]
    assert body["size_bytes"] > 0
    dataset_id = body["dataset_id"]

    listed = client.get(f"/models/{model_id}/datasets")
    assert listed.status_code == 200
    assert [d["dataset_id"] for d in listed.json()["datasets"]] == [dataset_id]

    got = client.get(f"/models/{model_id}/datasets/{dataset_id}")
    assert got.status_code == 200
    assert got.json()["dataset_id"] == dataset_id

    deleted = client.delete(f"/models/{model_id}/datasets/{dataset_id}")
    assert deleted.status_code == 204

    missing = client.get(f"/models/{model_id}/datasets/{dataset_id}")
    assert missing.status_code == 404


def test_upload_against_unknown_model_returns_404(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    client = TestClient(app)
    resp = client.post(
        "/models/does-not-exist/datasets",
        files={"file": ("history.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert resp.status_code == 404


def test_repeat_uploads_get_independent_ids(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    client = TestClient(app)
    model_id = _model_id(client, "SimpleVAVZone")

    first = client.post(
        f"/models/{model_id}/datasets",
        files={"file": ("history.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    second = client.post(
        f"/models/{model_id}/datasets",
        files={"file": ("history.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    assert first["dataset_id"] != second["dataset_id"]


def test_delete_blocked_while_referenced_by_a_job(monkeypatch, tmp_path):
    manager = _isolate(monkeypatch, tmp_path)
    client = TestClient(app)
    model_id = _model_id(client, "SimpleVAVZone")

    uploaded = client.post(
        f"/models/{model_id}/datasets",
        files={"file": ("history.csv", b"a,b\n1,2\n", "text/csv")},
    ).json()
    dataset_id = uploaded["dataset_id"]

    # A referencing job.json doesn't need a real calibration run -- the
    # delete guard only scans <storage_root>/<model_id>/jobs/*/job.json.
    job_dir = tmp_path / model_id / "jobs" / "cal_fake0001"
    job_dir.mkdir(parents=True)
    (job_dir / "job.json").write_text(
        '{"id": "cal_fake0001", "model_id": "%s", "dataset_id": "%s"}' % (model_id, dataset_id),
        encoding="utf-8",
    )

    blocked = client.delete(f"/models/{model_id}/datasets/{dataset_id}")
    assert blocked.status_code == 409

    still_there = client.get(f"/models/{model_id}/datasets/{dataset_id}")
    assert still_there.status_code == 200


def test_upload_failure_leaves_no_dataset_or_temp_file(monkeypatch, tmp_path):
    _isolate(monkeypatch, tmp_path)
    client = TestClient(app)
    model_id = _model_id(client, "SimpleVAVZone")

    async def _broken_read(self, size=-1):
        raise RuntimeError("simulated stream failure")

    from starlette.datastructures import UploadFile

    monkeypatch.setattr(UploadFile, "read", _broken_read)

    resp = client.post(
        f"/models/{model_id}/datasets",
        files={"file": ("history.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert resp.status_code == 500

    listed = client.get(f"/models/{model_id}/datasets")
    assert listed.status_code == 200
    assert listed.json()["datasets"] == []

    datasets_dir = tmp_path / model_id / "datasets"
    leftovers = list(datasets_dir.glob("*")) if datasets_dir.exists() else []
    assert leftovers == []

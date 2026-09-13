"""Database.create_/update_/get_/list_calibration_run(s) -- the CRUD layer over the
calibration_runs table (migration 033). The table is the BACnet Simulator's durable
workflow/audit record for one FMU calibration; these helpers own its JSON (de)coding and
guard the dynamic column interpolation against unknown column names.
"""
from __future__ import annotations

import pytest


def test_create_defaults_and_json_round_trip(database):
    run = database.create_calibration_run(
        model_id="rtu-guid",
        recording_id=7,
        tuner_names=["COP_nominal", "kSAT"],
        mapping_json={
            "supply_air_temp_c": {
                "recording_point_id": 18,
                "semantic_id": "supply_air_temperature",
                "selection_source": "user_override",
                "semantic_resolution": {"provider": "brick", "quality": "ambiguous", "alternatives": [12, 18]},
            }
        },
    )

    assert run["id"] > 0
    assert run["model_id"] == "rtu-guid"
    assert run["status"] == "pending"
    assert run["validation_status"] == "not_requested"
    assert run["schema_version"] == 1
    # JSON columns come back as real Python structures, not strings.
    assert run["tuner_names"] == ["COP_nominal", "kSAT"]
    assert run["mapping_json"]["supply_air_temp_c"]["semantic_resolution"]["quality"] == "ambiguous"
    assert run["best_parameters_json"] == {}
    assert run["fmu_defaults_json"] == {}

    fetched = database.get_calibration_run(run["id"])
    assert fetched == run


def test_create_requires_model_id(database):
    with pytest.raises(ValueError, match="model_id"):
        database.create_calibration_run(recording_id=1)


def test_unknown_column_is_rejected(database):
    with pytest.raises(ValueError, match="Unknown calibration_runs column"):
        database.create_calibration_run(model_id="m", bogus_column=1)
    run = database.create_calibration_run(model_id="m")
    with pytest.raises(ValueError, match="Unknown calibration_runs column"):
        database.update_calibration_run(run["id"], not_a_column="x")


def test_update_patches_only_named_columns(database):
    run = database.create_calibration_run(model_id="m", recording_id=3)

    updated = database.update_calibration_run(
        run["id"],
        status="queued",
        runtime_job_id="cal_abc123",
        runtime_dataset_id="ds_1",
        validation_status="pending",
        started_at="2026-09-09T12:00:00",
    )
    assert updated["status"] == "queued"
    assert updated["runtime_job_id"] == "cal_abc123"
    assert updated["validation_status"] == "pending"
    assert updated["recording_id"] == 3  # untouched

    synced = database.update_calibration_run(
        run["id"],
        status="completed",
        completed_at="2026-09-09T12:30:00",
        baseline_metric=25.0,
        calibration_metric=12.5,
        improvement_pct=50.0,
        validation_status="completed",
        validation_cv_rmse=18.0,
        validation_nmbe=-2.6,
        g14_interval="hourly",
        g14_based_on="validation",
        g14_applicable=1,
        g14_pass=1,
        best_parameters_json={"COP_nominal": 3.9},
        fmu_defaults_json={"COP_nominal": 3.5},
    )
    assert synced["status"] == "completed"
    assert synced["validation_cv_rmse"] == 18.0
    assert synced["g14_based_on"] == "validation"
    assert synced["best_parameters_json"] == {"COP_nominal": 3.9}

    assert database.update_calibration_run(run["id"]) == synced  # empty patch is a no-op read


def test_update_missing_run_returns_none(database):
    assert database.update_calibration_run(999999, status="completed") is None


def test_get_by_runtime_job(database):
    a = database.create_calibration_run(model_id="m", runtime_job_id="cal_a")
    database.create_calibration_run(model_id="m", runtime_job_id="cal_b")

    assert database.get_calibration_run_by_runtime_job("cal_a")["id"] == a["id"]
    assert database.get_calibration_run_by_runtime_job("cal_missing") is None


def test_list_filters_and_orders_newest_first(database):
    r1 = database.create_calibration_run(model_id="rtu", recording_id=1, simulation_model_id=100)
    r2 = database.create_calibration_run(model_id="rtu", recording_id=2, simulation_model_id=200)
    r3 = database.create_calibration_run(model_id="ahu", recording_id=1, simulation_model_id=100)

    all_ids = [r["id"] for r in database.list_calibration_runs()]
    assert all_ids == [r3["id"], r2["id"], r1["id"]]  # id DESC

    assert [r["id"] for r in database.list_calibration_runs(model_id="rtu")] == [r2["id"], r1["id"]]
    assert [r["id"] for r in database.list_calibration_runs(simulation_model_id=100)] == [r3["id"], r1["id"]]
    assert [r["id"] for r in database.list_calibration_runs(recording_id=2)] == [r2["id"]]
    assert database.list_calibration_runs(model_id="none") == []


def test_duplicate_runtime_job_id_is_rejected(database):
    database.create_calibration_run(model_id="m", runtime_job_id="cal_dup")
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        database.create_calibration_run(model_id="m", runtime_job_id="cal_dup")
    # ...but many NULLs are fine (row exists before its runtime job is created).
    database.create_calibration_run(model_id="m")
    database.create_calibration_run(model_id="m")

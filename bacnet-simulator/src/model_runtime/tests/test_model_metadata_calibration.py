"""ModelMetadata.calibration -- passthrough of model.json's "calibration"
block (goal + tuners) so a caller (e.g. bacnet-simulator's calibration
glue) can discover the calibration goal and whether a model supports
calibration at all, without a second model.json parser. Covers only the
new field/derived flag -- inputs/outputs/string_parameters parsing is
unchanged and already covered elsewhere."""
from __future__ import annotations

import json
from pathlib import Path

from shared.runtime.models.catalog import ModelMetadata

_BASE_MODEL_JSON = {
    "id": "test-model",
    "slug": "TestModel",
    "label": "Test Model",
    "fmu_path": "model.fmu",
    "inputs": [],
    "outputs": [{"name": "supply_air_temp_c", "fmu_variable": "TSup", "unit": "degC"}],
}


def _write_model_json(tmp_path: Path, **overrides) -> Path:
    data = {**_BASE_MODEL_JSON, **overrides}
    path = tmp_path / "model.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_calibration_block_round_trips_through_to_dict(tmp_path):
    calibration = {
        "enabled": True,
        "goal": {"output": "supply_air_temp_c", "metric": "cv_rmse"},
        "tuners": [{"parameter": "COP_nominal", "min": 2.5, "max": 5.0}],
    }
    metadata = ModelMetadata.from_file(_write_model_json(tmp_path, calibration=calibration))

    assert metadata.calibration == calibration
    assert metadata.to_dict()["calibration"] == calibration


def test_calibration_enabled_flag_true_when_block_says_enabled(tmp_path):
    calibration = {"enabled": True, "goal": {"output": "supply_air_temp_c"}, "tuners": []}
    metadata = ModelMetadata.from_file(_write_model_json(tmp_path, calibration=calibration))

    assert metadata.summary()["calibration_enabled"] is True


def test_calibration_enabled_flag_false_when_block_absent(tmp_path):
    metadata = ModelMetadata.from_file(_write_model_json(tmp_path))

    assert metadata.calibration is None
    assert metadata.summary()["calibration_enabled"] is False
    assert metadata.to_dict()["calibration"] is None


def test_calibration_enabled_flag_false_when_explicitly_disabled(tmp_path):
    calibration = {"enabled": False, "goal": {"output": "supply_air_temp_c"}, "tuners": []}
    metadata = ModelMetadata.from_file(_write_model_json(tmp_path, calibration=calibration))

    assert metadata.summary()["calibration_enabled"] is False

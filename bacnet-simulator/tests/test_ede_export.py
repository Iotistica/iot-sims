"""Export EDE options: "Configuration defaults" (existing/unchanged
behavior) vs "Current live values" (new mode, populates the
Present-Value-Default column from each object's current Present_Value at
export time instead of its configured/default behavior value).

See src/bacnet/ede.py::devices_to_ede's live_values param and
src/api/routers/exports.py's mode query param on GET
/devices/{id}/export/ede.

Two layers, mirroring test_brick_export.py's split:
  - Pure devices_to_ede() unit tests: hand-built device/object dicts, no DB.
  - HTTP-level tests: real device/object creation via the `client` fixture,
    engine live values supplied through conftest.py's _FakeEngine.live_values.
"""
from __future__ import annotations

import csv

from src.bacnet import ede


# ═══════════════════════════════════════════════════════════════════════════
# Pure devices_to_ede() unit tests
# ═══════════════════════════════════════════════════════════════════════════

def _device_with_objects(*objects: dict) -> dict:
    return {
        "device_instance": 100,
        "name": "Test Device",
        "description": "",
        "objects": list(objects),
    }


def _analog_object(id_: int, *, behavior_params: str = '{"value": 21.5}', units: str = "degrees-celsius") -> dict:
    return {
        "id": id_,
        "object_type": "analog-value",
        "object_instance": 1,
        "name": "Zone-Temp",
        "behavior": "constant",
        "behavior_params": behavior_params,
        "units": units,
    }


def _binary_object(id_: int, *, behavior_params: str = '{"value": true}') -> dict:
    return {
        "id": id_,
        "object_type": "binary-value",
        "object_instance": 1,
        "name": "Fan-Status",
        "behavior": "constant",
        "behavior_params": behavior_params,
        "units": "no-units",
    }


def _multistate_object(id_: int, *, behavior_params: str = '{"value": 2}') -> dict:
    return {
        "id": id_,
        "object_type": "multi-state-value",
        "object_instance": 1,
        "name": "Mode",
        "behavior": "constant",
        "behavior_params": behavior_params,
        "units": "no-units",
    }


def _rows(csv_text: str) -> list[dict]:
    lines = [ln for ln in csv_text.splitlines() if ln.strip() and not ln.startswith("#")]
    return list(csv.DictReader(lines, delimiter=";"))


def test_defaults_mode_unchanged_without_live_values():
    """live_values omitted entirely -- byte-for-byte the original
    configuration-defaults export behavior."""
    device = _device_with_objects(_analog_object(1))
    rows = _rows(ede.devices_to_ede([device], "Test"))
    assert rows[0]["Present-Value-Default"] == "21.5"


def test_live_mode_uses_current_value_instead_of_configured_default():
    device = _device_with_objects(_analog_object(1, behavior_params='{"value": 21.5}'))
    rows = _rows(ede.devices_to_ede([device], "Test", live_values={1: 23.75}))
    assert rows[0]["Present-Value-Default"] == "23.75"


def test_live_mode_blank_when_no_live_value_exists():
    """The actual requirement: no invented fallback to the configured
    default when there's no live value for this point."""
    device = _device_with_objects(_analog_object(1, behavior_params='{"value": 21.5}'))
    rows = _rows(ede.devices_to_ede([device], "Test", live_values={}))
    assert rows[0]["Present-Value-Default"] == ""


def test_live_mode_blank_when_live_value_is_none():
    """FMU/provider-owned point that has never ticked -- present in the
    dict but explicitly None, not just absent."""
    device = _device_with_objects(_analog_object(1))
    rows = _rows(ede.devices_to_ede([device], "Test", live_values={1: None}))
    assert rows[0]["Present-Value-Default"] == ""


def test_live_mode_binary_true_exports_as_one():
    device = _device_with_objects(_binary_object(1))
    rows = _rows(ede.devices_to_ede([device], "Test", live_values={1: True}))
    assert rows[0]["Present-Value-Default"] == "1.0"


def test_live_mode_binary_false_exports_as_zero():
    device = _device_with_objects(_binary_object(1))
    rows = _rows(ede.devices_to_ede([device], "Test", live_values={1: False}))
    assert rows[0]["Present-Value-Default"] == "0.0"


def test_live_mode_multistate_exports_current_state():
    device = _device_with_objects(_multistate_object(1))
    rows = _rows(ede.devices_to_ede([device], "Test", live_values={1: 3}))
    assert rows[0]["Present-Value-Default"] == "3.0"


def test_live_mode_does_not_change_any_other_column():
    """Schema/header/metadata identical between modes -- only
    Present-Value-Default differs."""
    device = _device_with_objects(_analog_object(1))
    defaults_rows = _rows(ede.devices_to_ede([device], "Test"))
    live_rows = _rows(ede.devices_to_ede([device], "Test", live_values={1: 99.0}))
    assert list(defaults_rows[0].keys()) == ede.EDE_HEADER
    for col in ede.EDE_HEADER:
        if col == "Present-Value-Default":
            continue
        assert defaults_rows[0][col] == live_rows[0][col]


def test_live_mode_multiple_objects_independent():
    device = _device_with_objects(
        _analog_object(1, behavior_params='{"value": 10.0}'),
        _analog_object(2, behavior_params='{"value": 20.0}'),
    )
    rows = _rows(ede.devices_to_ede([device], "Test", live_values={1: 99.0}))
    # Object 1 has a live value; object 2 doesn't -- each resolves
    # independently, no cross-contamination.
    assert rows[0]["Present-Value-Default"] == "99.0"
    assert rows[1]["Present-Value-Default"] == ""


# ═══════════════════════════════════════════════════════════════════════════
# HTTP-level tests: GET /devices/{id}/export/ede?mode=...
# ═══════════════════════════════════════════════════════════════════════════

def _create_device_with_object(client, *, instance: int) -> tuple[dict, dict]:
    device = client.post("/devices", json={"device_instance": instance, "name": "RTU-Test"}).json()
    obj = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": "analog-value",
        "object_instance": 1,
        "name": "Supply-Temp",
        "units": "degrees-celsius",
        "behavior": "constant",
        "behavior_params": '{"value": 20.0}',
    }).json()
    return device, obj


def test_export_default_mode_matches_existing_behavior(client, database):
    device, obj = _create_device_with_object(client, instance=9001)

    resp = client.get(f"/devices/{device['id']}/export/ede")

    assert resp.status_code == 200
    rows = _rows(resp.text)
    assert rows[0]["Present-Value-Default"] == "20.0"


def test_export_mode_query_param_explicit_defaults(client, database):
    device, obj = _create_device_with_object(client, instance=9002)

    resp = client.get(f"/devices/{device['id']}/export/ede?mode=defaults")

    assert resp.status_code == 200
    rows = _rows(resp.text)
    assert rows[0]["Present-Value-Default"] == "20.0"


def test_export_live_mode_uses_engines_current_value(client, database):
    device, obj = _create_device_with_object(client, instance=9003)
    client.app.state.engine.live_values[obj["id"]] = 24.5

    resp = client.get(f"/devices/{device['id']}/export/ede?mode=live")

    assert resp.status_code == 200
    rows = _rows(resp.text)
    assert rows[0]["Present-Value-Default"] == "24.5"


def test_export_live_mode_blank_when_engine_has_no_value_yet(client, database):
    """FMU/provider-owned point that hasn't ticked -- must be blank, not
    fall back to the configured default (20.0)."""
    device, obj = _create_device_with_object(client, instance=9004)
    # client.app.state.engine.live_values left empty on purpose.

    resp = client.get(f"/devices/{device['id']}/export/ede?mode=live")

    assert resp.status_code == 200
    rows = _rows(resp.text)
    assert rows[0]["Present-Value-Default"] == ""


def test_export_invalid_mode_rejected(client, database):
    device, obj = _create_device_with_object(client, instance=9005)

    resp = client.get(f"/devices/{device['id']}/export/ede?mode=bogus")

    assert resp.status_code == 422


def test_export_ede_missing_device_404(client, database):
    resp = client.get("/devices/999999/export/ede")
    assert resp.status_code == 404

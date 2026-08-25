"""PUT /devices/{id}/objects/{id} used to log a generic "configuration
updated" (or just "enabled"/"disabled"/"behavior changed to X") on every
edit, with no way to tell what an edit actually did without
cross-referencing the DB. update_object() now logs a field-level diff via
_describe_object_changes() -- e.g. "units 'percent' -> 'no-units'" --
covering every editable scalar field plus a shallow diff of
behavior_params, while excluding the fault-restore feature's internal
pre_fault_behavior/pre_fault_params bookkeeping keys (see
test_fault_restore.py) from the noise.
"""
from __future__ import annotations

import json


def _create_device_and_point(client, *, instance: int, **overrides):
    device = client.post("/devices", json={"device_instance": instance, "name": f"Device-{instance}"}).json()
    payload = {
        "object_type": "analog-value", "object_instance": 1, "name": "Test-Point",
        "units": "percent", "behavior": "constant", "behavior_params": '{"value": 10}',
    }
    payload.update(overrides)
    obj = client.post(f"/devices/{device['id']}/objects", json=payload).json()
    return device, obj


def _last_message(client) -> str:
    events = client.app.state.logged_events
    assert events, "expected a log entry"
    return events[-1]["message"]


def _put(client, device_id: int, obj: dict, **overrides) -> dict:
    payload = {
        "object_type": obj["object_type"], "object_instance": obj["object_instance"],
        "name": obj["name"], "units": obj.get("units", "no-units"),
        "behavior": obj["behavior"], "behavior_params": obj["behavior_params"],
        "enabled": obj.get("enabled", 1),
    }
    payload.update(overrides)
    resp = client.put(f"/devices/{device_id}/objects/{obj['id']}", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_units_change_is_described_with_old_and_new_value(client):
    device, obj = _create_device_and_point(client, instance=9201)
    client.app.state.logged_events.clear()

    _put(client, device["id"], obj, units="no-units")

    message = _last_message(client)
    assert "units" in message
    assert "'percent'" in message
    assert "'no-units'" in message


def test_reliability_change_is_described(client):
    device, obj = _create_device_and_point(client, instance=9202)
    client.app.state.logged_events.clear()

    _put(client, device["id"], obj, reliability="over-range")

    message = _last_message(client)
    assert "reliability" in message
    assert "'no-fault-detected'" in message
    assert "'over-range'" in message


def test_behavior_params_value_change_is_described(client):
    """The literal example from the report: seeing what a constant's value
    actually changed from/to, not just "configuration updated"."""
    device, obj = _create_device_and_point(client, instance=9203, behavior_params='{"value": 22}')
    client.app.state.logged_events.clear()

    _put(client, device["id"], obj, behavior_params='{"value": 45}')

    message = _last_message(client)
    assert "params:" in message
    assert "value 22 -> 45" in message


def test_multiple_field_changes_are_all_listed(client):
    device, obj = _create_device_and_point(client, instance=9204)
    client.app.state.logged_events.clear()

    _put(client, device["id"], obj, units="cubic-feet-per-minute", reliability="over-range")

    message = _last_message(client)
    assert "units" in message
    assert "reliability" in message


def test_no_actual_change_still_logs_generic_message(client):
    device, obj = _create_device_and_point(client, instance=9205)
    client.app.state.logged_events.clear()

    _put(client, device["id"], obj)  # identical payload, nothing changed

    assert _last_message(client) == "Object Test-Point: configuration updated"


def test_enabled_toggle_is_described(client):
    device, obj = _create_device_and_point(client, instance=9206)
    client.app.state.logged_events.clear()

    _put(client, device["id"], obj, enabled=0)

    message = _last_message(client)
    assert "enabled" in message
    assert "True" in message and "False" in message


def test_fault_restore_bookkeeping_keys_excluded_from_params_diff(client):
    """pre_fault_behavior/pre_fault_params are update_object()'s own
    internal snapshot fields (see test_fault_restore.py) -- entering fault
    must not spam the log with them, only with fields the caller actually
    intended to change."""
    device, obj = _create_device_and_point(client, instance=9207, behavior_params='{"value": 22}')
    client.app.state.logged_events.clear()

    faulted = _put(
        client, device["id"], obj,
        behavior="fault",
        behavior_params=json.dumps({
            "base_behavior": "constant", "base_params": {"value": 999},
            "fault_type": "stuck", "fault_value": 30, "mtbf_minutes": 60, "fault_duration_seconds": 30,
        }),
    )
    # Confirm the snapshot really did land (test_fault_restore.py's own
    # concern) so this test is exercising the real code path.
    assert "pre_fault_behavior" in json.loads(faulted["behavior_params"])

    message = _last_message(client)
    assert "pre_fault" not in message
    assert "behavior" in message
    assert "'constant'" in message and "'fault'" in message

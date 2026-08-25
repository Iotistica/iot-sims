""""Restore Previous"/"Return to Normal" for FMU-backed points: switching a
point to `fault` behavior (e.g. to test stuck-value handling) now
auto-snapshots its pre-fault behavior/params, and POST
.../restore-behavior puts it back exactly -- see objects.py's
update_object()/restore_object_behavior().

pre_fault_behavior/pre_fault_params are deliberately separate fields from
FaultBehavior's own base_behavior/base_params (behaviors.py) -- those mean
"what to compute between fault injections while fault is still active",
a real independently-configurable choice that restore must never
overwrite or be confused with.
"""
from __future__ import annotations

import json

from src.simulation.model_store import ensure_simulation_model_schema
from src.simulation.engine import SimEngine


def _make_device_and_point(client, *, instance: int, behavior: str, behavior_params: dict, object_type: str = "analog-value"):
    device = client.post("/devices", json={"device_instance": instance, "name": f"Device-{instance}"}).json()
    obj = client.post(f"/devices/{device['id']}/objects", json={
        "object_type": object_type, "object_instance": 1, "name": "Test-Point", "units": "percent",
        "behavior": behavior, "behavior_params": json.dumps(behavior_params),
    }).json()
    return device, obj


def _mark_provider_owned(database, point_id: int) -> int:
    """Minimal simulation_model_configs/mappings rows to make a point
    "provider-owned" per get_output_owners_by_point -- mirrors
    test_provider_owned_raw_behavior_migration.py's own helper."""
    ensure_simulation_model_schema(database)
    conn = database._conn()
    conn.execute(
        "INSERT INTO simulation_model_configs (name, provider_type, model_type, enabled) VALUES (?, 'fmu', 'RTU', 1)",
        (f"model-for-{point_id}",),
    )
    model_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO simulation_model_mappings (model_config_id, variable, direction, point_id) "
        "VALUES (?, 'some_variable', 'output', ?)",
        (model_id, point_id),
    )
    conn.commit()
    conn.close()
    return model_id


def _fault_payload(base_object: dict, **overrides) -> dict:
    payload = {
        "object_type": base_object["object_type"],
        "object_instance": base_object["object_instance"],
        "name": base_object["name"],
        "units": base_object.get("units", "percent"),
        "behavior": "fault",
        "behavior_params": json.dumps({
            "base_behavior": "constant", "base_params": {"value": 999},
            "fault_type": "stuck", "fault_value": 30, "mtbf_minutes": 60, "fault_duration_seconds": 30,
        }),
        "enabled": 1,
    }
    payload.update(overrides)
    return payload


# ═══════════════════════════════════════════════════════════════════════════
# 1. raw -> fault -> restore raw (provider-owned point)
# ═══════════════════════════════════════════════════════════════════════════

def test_raw_provider_owned_point_restores_to_raw_after_fault(client, database):
    device, obj = _make_device_and_point(
        client, instance=9101, behavior="raw", behavior_params={"value": 64.0},
    )
    _mark_provider_owned(database, obj["id"])

    resp = client.put(f"/devices/{device['id']}/objects/{obj['id']}", json=_fault_payload(obj))
    assert resp.status_code == 200, resp.text
    faulted = resp.json()
    assert faulted["behavior"] == "fault"
    fault_params = json.loads(faulted["behavior_params"])
    assert fault_params["pre_fault_behavior"] == "raw"
    assert fault_params["pre_fault_params"] == {"value": 64.0}
    # base_behavior/base_params (a real, independent field the drawer's own
    # editor set) must be left completely alone by the auto-snapshot.
    assert fault_params["base_behavior"] == "constant"
    assert fault_params["base_params"] == {"value": 999}

    resp = client.post(f"/devices/{device['id']}/objects/{obj['id']}/restore-behavior")
    assert resp.status_code == 200, resp.text
    restored = resp.json()
    assert restored["behavior"] == "raw"
    assert json.loads(restored["behavior_params"]) == {"value": 64.0}


# ═══════════════════════════════════════════════════════════════════════════
# 2. constant -> fault -> restore constant/value (non-provider point)
# ═══════════════════════════════════════════════════════════════════════════

def test_constant_point_restores_to_constant_with_original_value(client, database):
    device, obj = _make_device_and_point(
        client, instance=9102, behavior="constant", behavior_params={"value": 45.5},
    )

    resp = client.put(f"/devices/{device['id']}/objects/{obj['id']}", json=_fault_payload(obj))
    assert resp.status_code == 200, resp.text

    resp = client.post(f"/devices/{device['id']}/objects/{obj['id']}/restore-behavior")
    assert resp.status_code == 200, resp.text
    restored = resp.json()
    assert restored["behavior"] == "constant"
    assert json.loads(restored["behavior_params"]) == {"value": 45.5}


# ═══════════════════════════════════════════════════════════════════════════
# 3. schedule -> fault -> restore schedule
# ═══════════════════════════════════════════════════════════════════════════

def test_schedule_point_restores_to_schedule(client, database):
    schedule_params = {"default": 18, "blocks": [{"start": "07:00", "value": 22}, {"start": "18:00", "value": 18}]}
    device, obj = _make_device_and_point(
        client, instance=9103, behavior="schedule", behavior_params=schedule_params,
    )

    resp = client.put(f"/devices/{device['id']}/objects/{obj['id']}", json=_fault_payload(obj))
    assert resp.status_code == 200, resp.text
    fault_params = json.loads(resp.json()["behavior_params"])
    assert fault_params["pre_fault_behavior"] == "schedule"
    assert fault_params["pre_fault_params"] == schedule_params

    resp = client.post(f"/devices/{device['id']}/objects/{obj['id']}/restore-behavior")
    assert resp.status_code == 200, resp.text
    restored = resp.json()
    assert restored["behavior"] == "schedule"
    assert json.loads(restored["behavior_params"]) == schedule_params


# ═══════════════════════════════════════════════════════════════════════════
# Fallback: no usable snapshot -- never invent "raw" for a non-provider point
# ═══════════════════════════════════════════════════════════════════════════

def test_legacy_fault_with_no_snapshot_falls_back_to_constant_not_raw(client, database):
    """A fault created before this feature existed (no pre_fault_behavior
    in its params) on a point that is NOT provider-owned -- must fall back
    to "constant", never "raw"."""
    device, obj = _make_device_and_point(
        client, instance=9104, behavior="fault",
        behavior_params={"base_behavior": "constant", "base_params": {"value": 10}, "fault_type": "spike", "fault_value": 999, "mtbf_minutes": 60, "fault_duration_seconds": 30},
    )

    resp = client.post(f"/devices/{device['id']}/objects/{obj['id']}/restore-behavior")
    assert resp.status_code == 200, resp.text
    restored = resp.json()
    assert restored["behavior"] == "constant"
    assert restored["behavior"] != "raw"


def test_legacy_fault_with_no_snapshot_falls_back_to_raw_for_provider_owned_point(client, database):
    device, obj = _make_device_and_point(
        client, instance=9105, behavior="fault",
        behavior_params={"base_behavior": "constant", "base_params": {"value": 10}, "fault_type": "spike", "fault_value": 999, "mtbf_minutes": 60, "fault_duration_seconds": 30},
    )
    _mark_provider_owned(database, obj["id"])

    resp = client.post(f"/devices/{device['id']}/objects/{obj['id']}/restore-behavior")
    assert resp.status_code == 200, resp.text
    assert resp.json()["behavior"] == "raw"


def test_restore_rejected_when_not_currently_faulted(client, database):
    device, obj = _make_device_and_point(
        client, instance=9106, behavior="constant", behavior_params={"value": 1.0},
    )
    resp = client.post(f"/devices/{device['id']}/objects/{obj['id']}/restore-behavior")
    assert resp.status_code == 409


# ═══════════════════════════════════════════════════════════════════════════
# In-fault edits (e.g. tweaking fault_value) must not re-snapshot and lose
# the original pre-fault state
# ═══════════════════════════════════════════════════════════════════════════

def test_editing_fault_settings_again_does_not_reclobber_the_snapshot(client, database):
    device, obj = _make_device_and_point(
        client, instance=9107, behavior="constant", behavior_params={"value": 12.0},
    )
    resp = client.put(f"/devices/{device['id']}/objects/{obj['id']}", json=_fault_payload(obj))
    assert resp.status_code == 200, resp.text

    # Still "fault" -> "fault": tweak fault_value only. The (now-current)
    # behavior_params sent by a real edit already carries pre_fault_* from
    # the previous response -- simulate that faithfully rather than
    # re-sending a hand-built payload missing it.
    current = client.get(f"/devices/{device['id']}/objects/{obj['id']}").json()
    params = json.loads(current["behavior_params"])
    params["fault_value"] = 55
    resp = client.put(f"/devices/{device['id']}/objects/{obj['id']}", json=_fault_payload(obj, behavior_params=json.dumps(params)))
    assert resp.status_code == 200, resp.text
    assert json.loads(resp.json()["behavior_params"])["pre_fault_behavior"] == "constant"
    assert json.loads(resp.json()["behavior_params"])["pre_fault_params"] == {"value": 12.0}

    resp = client.post(f"/devices/{device['id']}/objects/{obj['id']}/restore-behavior")
    assert resp.status_code == 200, resp.text
    restored = resp.json()
    assert restored["behavior"] == "constant"
    assert json.loads(restored["behavior_params"]) == {"value": 12.0}


# ═══════════════════════════════════════════════════════════════════════════
# Restoration must not touch simulation_model_mappings/provider ownership
# ═══════════════════════════════════════════════════════════════════════════

def test_restore_does_not_touch_simulation_model_mappings(client, database):
    device, obj = _make_device_and_point(
        client, instance=9108, behavior="raw", behavior_params={"value": 70.0},
    )
    _mark_provider_owned(database, obj["id"])

    with database._conn() as conn:
        before = [dict(r) for r in conn.execute(
            "SELECT * FROM simulation_model_mappings WHERE point_id=?", (obj["id"],)
        ).fetchall()]

    client.put(f"/devices/{device['id']}/objects/{obj['id']}", json=_fault_payload(obj))
    client.post(f"/devices/{device['id']}/objects/{obj['id']}/restore-behavior")

    with database._conn() as conn:
        after = [dict(r) for r in conn.execute(
            "SELECT * FROM simulation_model_mappings WHERE point_id=?", (obj["id"],)
        ).fetchall()]

    assert before == after


# ═══════════════════════════════════════════════════════════════════════════
# No zero-value reset during restore -- engine level, exercising the real
# reload()/_create_object() preservation path (client/database's default
# _FakeEngine has a no-op reload(), so this needs a real SimEngine).
# ═══════════════════════════════════════════════════════════════════════════

async def test_restore_to_raw_does_not_reset_provider_owned_point_to_zero(database):
    """Mirrors what restore_object_behavior() actually persists for a
    provider-owned point (behavior='raw', behavior_params='{}' -- raw's
    real value always comes from the live provider, never stored params)
    and confirms _create_object(), via the reload-preservation snapshot,
    seeds the point's last-known live value instead of 0."""
    engine = SimEngine(database)
    obj_id = 12345
    engine._point_output_owner[obj_id] = "fmu:RTU:1"
    # The value the point was actually showing right before restore (e.g.
    # the fault's stuck-low value, or the live provider value if the fault
    # had already lapsed) -- exactly what reload() would have snapshotted
    # from _prev_values immediately before restore_object_behavior()'s
    # schedule_engine_reload() call.
    engine._reload_preserved_values[obj_id] = 64.0

    obj_row = {
        "id": obj_id, "object_type": "analog-value", "object_instance": 1,
        "name": "Test-Point", "units": "percent",
        "behavior": "raw", "behavior_params": "{}",
        "manual_value": None, "reliability": "no-fault-detected",
    }
    bacnet_obj, _ = engine._create_object(obj_row, slot=0, device_name="RTU")

    assert float(bacnet_obj.presentValue) == 64.0
    assert float(bacnet_obj.presentValue) != 0.0

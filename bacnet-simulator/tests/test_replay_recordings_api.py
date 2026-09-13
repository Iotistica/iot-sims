"""HTTP layer for the Replay Recording router (src/api/routers/recordings.py)
-- thin, so this only checks the wiring and the "external devices only"
guard; the actual persistence/buffer/grouping logic is covered directly
against Database in the other test_replay_*.py files."""
from __future__ import annotations


def _make_external_device(database, *, instance: int, name: str) -> dict:
    # sync_external_devices returns EVERY external device (ordered by
    # device_instance), not just the one(s) just synced -- picking [0]
    # silently returns the wrong device once a test calls this more than
    # once, so match on the instance actually requested instead.
    devices = database.sync_external_devices([{
        "protocol": "bacnet", "device_instance": instance,
        "name": name,
        "host": "127.0.0.1",
        "port": 47808,
        "metadata": {},
    }])
    return next(d for d in devices if d["device_instance"] == instance)


def _make_simulated_device(client, *, instance: int, name: str) -> dict:
    return client.post("/devices", json={"protocol": "bacnet", "device_instance": instance, "name": name}).json()


def test_create_recording_allowed_for_simulated_device(client):
    # Recordings are a general-purpose, device-agnostic feature -- a
    # simulated device's points are sampled directly from its own live
    # in-process values (see runtime._recording_sample_once's
    # non-external branch) rather than over the wire, but creation is
    # otherwise identical, so this must succeed exactly like an external
    # device's does.
    device = _make_simulated_device(client, instance=9501, name="Not External")

    res = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Should Work",
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "overwrite",
    })

    assert res.status_code == 201
    assert res.json()["source_device_id"] == device["id"]


def test_create_recording_for_simulated_device_accepts_a_point_on_another_device(client, database):
    """Simulated/mirror/replay-source devices sample via SimEngine.
    get_object_value(source_object_id) -- a plain global lookup with no
    device scoping (see runtime.py's _sample_due_simulated_recordings) --
    so a point on a DIFFERENT device works exactly the same as one on this
    device. This is what lets a "Model points" recording capture a model's
    upstream inputs (e.g. an AHU feeding a zone model) alongside its own
    outputs."""
    device = _make_simulated_device(client, instance=9503, name="Zone Device")
    other_device = _make_simulated_device(client, instance=9504, name="Upstream AHU")
    foreign_point = database.create_object(other_device["id"], {
        "object_type": "analog-input", "object_instance": 0, "name": "Discharge Air Temp",
        "units": "degrees-celsius", "behavior": "constant", "behavior_params": '{"value":0}',
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected",
        "polarity": "normal", "point_type": None,
    })

    res = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Cross-Device Recording",
        "point_ids": [foreign_point["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "overwrite",
    })

    assert res.status_code == 201
    assert res.json()["point_count"] == 1


def test_create_recording_for_simulated_device_rejects_a_nonexistent_point_id(client):
    device = _make_simulated_device(client, instance=9505, name="Zone Device 2")

    res = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Bad Recording",
        "point_ids": [999999],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "overwrite",
    })

    assert res.status_code == 400


def test_create_recording_for_external_device_still_rejects_a_point_on_another_device(client, database):
    """External-BACnet devices sample over the wire from their own
    external_host/device_instance connection (see runtime.py's
    _recording_sample_once) -- a point belonging to a different
    device genuinely can't be read that way, so this source type stays
    restricted to its own objects, unlike the simulated case above."""
    device = _make_external_device(database, instance=9506, name="External A")
    other_device = _make_external_device(database, instance=9507, name="External B")
    foreign_point = database.create_object(other_device["id"], {
        "object_type": "analog-input", "object_instance": 0, "name": "B",
        "units": "degrees-celsius", "behavior": "constant", "behavior_params": '{"value":0}',
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected",
        "polarity": "normal", "point_type": None,
    })

    res = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Should Fail",
        "point_ids": [foreign_point["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "overwrite",
    })

    assert res.status_code == 400


def test_create_start_stop_delete_recording_via_api(client, database):
    device = _make_external_device(database, instance=9502, name="External Device")
    point = database.create_object(device["id"], {
        "object_type": "analog-input", "object_instance": 0, "name": "A",
        "units": "degrees-celsius", "behavior": "constant", "behavior_params": '{"value":0}',
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected",
        "polarity": "normal", "point_type": None,
    })

    created = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "API Recording",
        "point_ids": [point["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "overwrite",
    }).json()
    assert created["status"] == "recording"
    assert created["point_count"] == 1

    listed = client.get(f"/devices/{device['id']}/recordings").json()
    assert len(listed) == 1

    updated = client.put(f"/recordings/{created['id']}", json={
        "name": "Renamed Recording",
        "sample_interval_seconds": 10.0,
        "maximum_samples": 500,
        "buffer_mode": "stop",
    }).json()
    assert updated["name"] == "Renamed Recording"
    assert updated["sample_interval_seconds"] == 10.0
    assert updated["maximum_samples"] == 500
    assert updated["buffer_mode"] == "stop"
    assert updated["status"] == "recording"  # editing doesn't touch status
    assert updated["point_count"] == 1  # points untouched by edit

    stopped = client.post(f"/recordings/{created['id']}/stop").json()
    assert stopped["status"] == "completed"

    res = client.delete(f"/recordings/{created['id']}")
    assert res.status_code == 204
    assert client.get(f"/recordings/{created['id']}").status_code == 404


# ── condition (migration 025) ────────────────────────────────────────────

def test_create_recording_persists_each_condition_value(client):
    device = _make_simulated_device(client, instance=9503, name="Condition Device")
    for condition in ("normal", "degraded", "fault"):
        res = client.post(f"/devices/{device['id']}/recordings", json={
            "name": f"Rec {condition}", "condition": condition,
            "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
        })
        assert res.status_code == 201, res.text
        assert res.json()["condition"] == condition


def test_create_recording_without_condition_reads_back_null(client):
    """Backward compatibility: omitting condition entirely (the shape every
    pre-migration-025 client sends) must still succeed, never require it,
    never guess a value."""
    device = _make_simulated_device(client, instance=9504, name="No Condition Device")
    res = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Unclassified", "sample_interval_seconds": 5.0,
        "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert res.status_code == 201, res.text
    assert res.json()["condition"] is None


def test_create_recording_rejects_invalid_condition_value(client):
    device = _make_simulated_device(client, instance=9505, name="Bad Condition Device")
    res = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Bad", "condition": "faulty",  # not one of normal/degraded/fault
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert res.status_code == 422


def test_update_recording_condition(client):
    device = _make_simulated_device(client, instance=9506, name="Update Condition Device")
    created = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Starts Normal", "condition": "normal",
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    }).json()
    assert created["condition"] == "normal"

    updated = client.put(f"/recordings/{created['id']}", json={
        "name": "Now Degraded", "condition": "degraded",
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["condition"] == "degraded"


def test_update_recording_rejects_invalid_condition_value(client):
    device = _make_simulated_device(client, instance=9507, name="Bad Update Condition Device")
    created = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Rec", "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    }).json()

    res = client.put(f"/recordings/{created['id']}", json={
        "name": "Rec", "condition": "severe",
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert res.status_code == 422


def test_pre_migration_recording_reads_back_condition_null(client, database):
    """A recording created directly at the DB layer with no condition ever
    set (simulating one that predates migration 025) must read back
    condition=None via the API, not error or a guessed value."""
    device = _make_simulated_device(client, instance=9508, name="Legacy Device")
    recording = database.create_recording(device["id"], {
        "name": "Pre-migration Recording", "point_ids": [],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert recording["condition"] is None


# ── fault_type (migration 030) ────────────────────────────────────────────
# Second classification axis alongside condition -- what kind of problem,
# not how bad. Values grounded in the real LBNL/ORNL RTU FDD benchmark
# dataset (see this feature's own plan for the mapping).

_FAULT_TYPES = (
    "sensor_bias", "sensor_stuck", "actuator_stuck", "actuator_leak",
    "control_unstable", "incorrect_setpoint",
    "simultaneous_heat_cool", "filter_fouling",
    "compressor_staging", "condenser_fouling", "evaporator_fouling",
    "refrigerant_overcharge", "refrigerant_undercharge", "refrigerant_line_restriction",
    "heat_exchanger_fouling", "water_flow_restriction", "cavitation",
    "ignition_failure", "low_water_cutoff_fault",
    "belt_or_drive_fault",
)


def test_create_recording_persists_each_fault_type_value(client):
    device = _make_simulated_device(client, instance=9509, name="Fault Type Device")
    for i, fault_type in enumerate(_FAULT_TYPES):
        res = client.post(f"/devices/{device['id']}/recordings", json={
            "name": f"Rec {fault_type}", "condition": "fault", "fault_type": fault_type,
            "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
        })
        assert res.status_code == 201, res.text
        assert res.json()["fault_type"] == fault_type


def test_create_recording_without_fault_type_reads_back_null(client):
    device = _make_simulated_device(client, instance=9510, name="No Fault Type Device")
    res = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Unclassified", "condition": "fault", "sample_interval_seconds": 5.0,
        "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert res.status_code == 201, res.text
    assert res.json()["fault_type"] is None


def test_create_recording_rejects_invalid_fault_type_value(client):
    device = _make_simulated_device(client, instance=9511, name="Bad Fault Type Device")
    res = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Bad", "condition": "fault", "fault_type": "haunted",  # not a real value
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert res.status_code == 422


def test_update_recording_fault_type(client):
    device = _make_simulated_device(client, instance=9512, name="Update Fault Type Device")
    created = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Rec", "condition": "degraded", "fault_type": "sensor_bias",
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    }).json()
    assert created["fault_type"] == "sensor_bias"

    updated = client.put(f"/recordings/{created['id']}", json={
        "name": "Rec", "condition": "fault", "fault_type": "compressor_staging",
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["fault_type"] == "compressor_staging"


def test_update_recording_omitting_fault_type_clears_it(client):
    """update_recording is a full-replace PUT, same contract `condition`
    has always had (never an "omitted means unchanged" merge like the
    dashboard-widget entities use) -- RecordingsDrawer.vue's own save()
    always resends both fields for exactly this reason. Omitting fault_type
    here clears it, it doesn't leave the old value in place."""
    device = _make_simulated_device(client, instance=9513, name="Clear Fault Type Device")
    created = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Rec", "condition": "fault", "fault_type": "evaporator_fouling",
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    }).json()

    updated = client.put(f"/recordings/{created['id']}", json={
        "name": "Renamed", "condition": "fault",
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert updated.status_code == 200, updated.text
    assert updated.json()["fault_type"] is None


def test_update_recording_rejects_invalid_fault_type_value(client):
    device = _make_simulated_device(client, instance=9514, name="Bad Update Fault Type Device")
    created = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Rec", "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    }).json()

    res = client.put(f"/recordings/{created['id']}", json={
        "name": "Rec", "fault_type": "gremlins",
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert res.status_code == 422


def test_pre_migration_recording_reads_back_fault_type_null(client, database):
    device = _make_simulated_device(client, instance=9515, name="Legacy Fault Type Device")
    recording = database.create_recording(device["id"], {
        "name": "Pre-migration Recording", "point_ids": [],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert recording["fault_type"] is None

    res = client.get(f"/recordings/{recording['id']}")
    assert res.status_code == 200
    assert res.json()["condition"] is None


# ── tags (migration 026) ─────────────────────────────────────────────────

def test_create_recording_persists_tags(client):
    device = _make_simulated_device(client, instance=9509, name="Tags Device")
    res = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Tagged", "tags": ["hot-day", "fan-test"],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert res.status_code == 201, res.text
    assert sorted(res.json()["tags"]) == ["fan-test", "hot-day"]


def test_create_recording_without_tags_reads_back_empty_list(client):
    """Backward compatibility: omitting tags entirely still succeeds, reads
    back [] rather than requiring it or erroring."""
    device = _make_simulated_device(client, instance=9510, name="No Tags Device")
    res = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Untagged", "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert res.status_code == 201, res.text
    assert res.json()["tags"] == []


def test_create_recording_dedupes_and_trims_tags_server_side(client):
    device = _make_simulated_device(client, instance=9511, name="Dupe Tags Device")
    res = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Dupes", "tags": [" hot-day ", "hot-day", "", "  ", "cooling"],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert res.status_code == 201, res.text
    assert sorted(res.json()["tags"]) == ["cooling", "hot-day"]


def test_update_recording_tags_replaces_the_full_set(client):
    device = _make_simulated_device(client, instance=9512, name="Update Tags Device")
    created = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Rec", "tags": ["hot-day"],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    }).json()
    assert created["tags"] == ["hot-day"]

    updated = client.put(f"/recordings/{created['id']}", json={
        "name": "Rec", "tags": ["cooling", "startup"],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert updated.status_code == 200, updated.text
    assert sorted(updated.json()["tags"]) == ["cooling", "startup"]

    # A subsequent update omitting tags entirely clears them -- update is a
    # full replace, same as it already is for every other editable field.
    cleared = client.put(f"/recordings/{created['id']}", json={
        "name": "Rec", "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "stop",
    })
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["tags"] == []


def test_pre_migration_recording_with_legacy_category_reads_back_as_one_tag(client, database):
    """A recording whose only classification data is the old (frozen,
    no-longer-written) `category` column, inserted directly to simulate one
    that predates migration 026 and was then backfilled -- reads back as a
    one-element tags list via the API, not the raw category string."""
    device = _make_simulated_device(client, instance=9513, name="Legacy Category Device")
    with database._conn() as conn:
        recording_id = conn.execute(
            "INSERT INTO recordings (source_device_id, name, category, status, "
            "sample_interval_seconds, maximum_samples, buffer_mode) "
            "VALUES (?, 'Legacy', 'Faulty', 'completed', 5.0, 10, 'stop')",
            (device["id"],),
        ).lastrowid
        conn.execute(
            "INSERT INTO recording_tags (recording_id, tag) VALUES (?, 'Faulty')",
            (recording_id,),
        )  # what migration 026's own backfill would have produced
        conn.commit()

    res = client.get(f"/recordings/{recording_id}")
    assert res.status_code == 200
    assert res.json()["tags"] == ["Faulty"]

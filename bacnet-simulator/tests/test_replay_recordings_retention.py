"""Recording retention (Database.purge_expired_recordings, driven by
recording_retention_loop in simulation/runtime.py). Only status='completed'
recordings are ever candidates -- a still-'recording' one has no ended_at at
all, so it's naturally excluded by the query regardless of age, matching the
design: it's actively in use (buffer_mode='overwrite' can run indefinitely
with no natural end), not stale.

Backdating ended_at directly via raw SQL (no API for this) -- stop_replay_
recording() only ever sets it to "now".
"""
from __future__ import annotations


def _make_simulated_device(client, *, instance: int, name: str) -> dict:
    return client.post("/devices", json={"protocol": "bacnet", "device_instance": instance, "name": name}).json()


def _make_completed_recording(client, database, *, device_id: int, name: str, age_days: int) -> int:
    recording = client.post(f"/devices/{device_id}/recordings", json={
        "name": name,
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "overwrite",
    }).json()
    database.stop_recording(recording["id"])
    with database._conn() as conn:
        conn.execute(
            "UPDATE recordings SET ended_at=datetime('now', ? || ' days') WHERE id=?",
            (f"-{age_days}", recording["id"]),
        )
        conn.commit()
    return recording["id"]


def test_purge_deletes_completed_recording_past_retention(client, database):
    device = _make_simulated_device(client, instance=9601, name="Retention-Dev-1")
    recording_id = _make_completed_recording(client, database, device_id=device["id"], name="Old", age_days=40)

    deleted = database.purge_expired_recordings(30)

    assert deleted == 1
    assert database.get_recording(recording_id) is None


def test_purge_skips_recording_within_retention(client, database):
    device = _make_simulated_device(client, instance=9602, name="Retention-Dev-2")
    recording_id = _make_completed_recording(client, database, device_id=device["id"], name="Recent", age_days=5)

    deleted = database.purge_expired_recordings(30)

    assert deleted == 0
    assert database.get_recording(recording_id) is not None


def test_purge_skips_recording_still_referenced_by_a_replay_device(client, database):
    device = _make_simulated_device(client, instance=9603, name="Retention-Dev-3")
    recording_id = _make_completed_recording(client, database, device_id=device["id"], name="In-Use", age_days=40)

    resp = client.post("/devices", json={
        "protocol": "bacnet", "device_instance": 9604, "name": "Replay-Dev-3",
        "simulation_mode": "replay", "active_recording_id": recording_id,
    })
    assert resp.status_code < 400, resp.text  # sanity: device creation succeeded

    deleted = database.purge_expired_recordings(30)

    assert deleted == 0
    assert database.get_recording(recording_id) is not None


def test_purge_never_touches_a_still_recording_status(client, database):
    device = _make_simulated_device(client, instance=9605, name="Retention-Dev-4")
    recording = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "Still-Going",
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "overwrite",
    }).json()
    # Backdate created_at/started_at (not ended_at -- there isn't one; the
    # recording was never stopped) far in the past.
    with database._conn() as conn:
        conn.execute(
            "UPDATE recordings SET created_at=datetime('now','-999 days'), "
            "started_at=datetime('now','-999 days') WHERE id=?",
            (recording["id"],),
        )
        conn.commit()

    deleted = database.purge_expired_recordings(30)

    assert deleted == 0
    assert database.get_recording(recording["id"]) is not None


def test_purge_cascades_samples_and_points(client, database):
    device = _make_simulated_device(client, instance=9606, name="Retention-Dev-5")
    point = database.create_object(device["id"], {
        "object_type": "analog-input", "object_instance": 0, "name": "A",
        "units": "degrees-celsius", "behavior": "constant", "behavior_params": '{"value":0}',
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected",
        "polarity": "normal", "point_type": None,
    })
    recording = client.post(f"/devices/{device['id']}/recordings", json={
        "name": "With-Samples", "point_ids": [point["id"]],
        "sample_interval_seconds": 5.0, "maximum_samples": 10, "buffer_mode": "overwrite",
    }).json()
    recording_point_id = recording["points"][0]["id"]
    database.add_recording_sample(recording["id"], {
        recording_point_id: {"value": 22.0, "reliability": "no-fault-detected", "out_of_service": False},
    })
    database.stop_recording(recording["id"])
    with database._conn() as conn:
        conn.execute(
            "UPDATE recordings SET ended_at=datetime('now','-40 days') WHERE id=?",
            (recording["id"],),
        )
        conn.commit()
        points_before = conn.execute(
            "SELECT COUNT(*) FROM recording_points WHERE recording_id=?", (recording["id"],),
        ).fetchone()[0]
        samples_before = conn.execute(
            "SELECT COUNT(*) FROM recording_samples WHERE recording_id=?", (recording["id"],),
        ).fetchone()[0]
    assert points_before == 1
    assert samples_before == 1

    deleted = database.purge_expired_recordings(30)
    assert deleted == 1

    with database._conn() as conn:
        points_after = conn.execute(
            "SELECT COUNT(*) FROM recording_points WHERE recording_id=?", (recording["id"],),
        ).fetchone()[0]
        samples_after = conn.execute(
            "SELECT COUNT(*) FROM recording_samples WHERE recording_id=?", (recording["id"],),
        ).fetchone()[0]
    assert points_after == 0
    assert samples_after == 0


def test_purge_zero_days_worth_of_recordings_is_a_noop_when_none_qualify(client, database):
    device = _make_simulated_device(client, instance=9607, name="Retention-Dev-6")
    _make_completed_recording(client, database, device_id=device["id"], name="Fresh", age_days=1)

    assert database.purge_expired_recordings(30) == 0

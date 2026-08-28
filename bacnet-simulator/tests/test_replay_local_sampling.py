"""runtime._sample_due_simulated_recordings -- sampling a simulated (or
mirror/replay) device's recording, tied to *simulated* time
(SimEngine.state.elapsed_seconds) rather than wall-clock polling.

Recordings are a general-purpose, device-agnostic feature: a simulated
device's points are already live in-process, so sampling reads them
directly via SimEngine.get_object_value(source_object_id) instead of
making a BACnet network read. This function is called from tick_loop right
after engine.tick() completes, so it only ever runs once per completed
tick -- covers that due-ness/no-duplication behavior. The external-bacnet
branch is covered separately in test_replay_recordings_api.py /
test_replay_recording_persistence.py's wall-clock-based paths.
"""
from __future__ import annotations

from src.simulation import runtime


def _make_device(client, *, instance: int, name: str) -> dict:
    return client.post("/devices", json={"device_instance": instance, "name": name}).json()


def _make_point(database, device_id: int, *, instance: int, name: str) -> dict:
    return database.create_object(device_id, {
        "object_type": "analog-input",
        "object_instance": instance,
        "name": name,
        "units": "degrees-celsius",
        "behavior": "constant",
        "behavior_params": '{"value":0}',
        "enabled": 1,
        "number_of_states": 2,
        "reliability": "no-fault-detected",
        "polarity": "normal",
        "point_type": None,
    })


class _StubState:
    def __init__(self, elapsed_seconds: float) -> None:
        self.elapsed_seconds = elapsed_seconds


class _StubEngine:
    """Just enough of SimEngine for the sampling branch: a fixed
    object_id -> value map (standing in for self._prev_values) plus a
    settable simulated-time clock."""

    def __init__(self, values: dict[int, float], elapsed_seconds: float = 100.0) -> None:
        self._values = values
        self.state = _StubState(elapsed_seconds)

    def get_object_value(self, object_id: int):
        return self._values.get(object_id)


async def test_local_device_sampled_from_engine_not_network(client, database):
    device = _make_device(client, instance=9801, name="Local Sim Device")
    point = _make_point(database, device["id"], instance=0, name="Zone Temp")

    recording = database.create_replay_recording(device["id"], {
        "name": "Local Sampling",
        "point_ids": [point["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "overwrite",
    })
    runtime._replay_last_sampled_sim_time.pop(recording["id"], None)

    engine = _StubEngine({point["id"]: 21.5}, elapsed_seconds=100.0)
    await runtime._sample_due_simulated_recordings(database, engine)

    updated = database.get_replay_recording(recording["id"])
    assert updated["sample_count"] == 1
    samples = database.get_replay_recording_samples(recording["id"], 0)
    # Stored as TEXT (matches trend_log_records.value's convention for
    # arbitrarily-typed present values) -- compare as a string.
    assert samples[0]["value"] == "21.5"
    assert runtime._replay_last_sampled_sim_time[recording["id"]] == 100.0


async def test_local_device_with_no_resolvable_points_is_skipped_without_sampling(
    client, database,
):
    # A recording_point whose source_object_id is None (e.g. its original
    # object was since deleted) shouldn't be sent to get_object_value, and
    # if that leaves nothing to sample, no (empty) sample row is written.
    device = _make_device(client, instance=9802, name="Local Sim Device 2")
    point = _make_point(database, device["id"], instance=0, name="Zone Temp")
    recording = database.create_replay_recording(device["id"], {
        "name": "Orphaned Point",
        "point_ids": [point["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "overwrite",
    })
    with database._conn() as conn:
        conn.execute(
            "UPDATE replay_recording_points SET source_object_id=NULL WHERE recording_id=?",
            (recording["id"],),
        )
        conn.commit()
    runtime._replay_last_sampled_sim_time.pop(recording["id"], None)

    engine = _StubEngine({}, elapsed_seconds=100.0)
    await runtime._sample_due_simulated_recordings(database, engine)

    updated = database.get_replay_recording(recording["id"])
    assert updated["sample_count"] == 0
    # Still marked as sampled this cycle (at this simulated time) so it
    # doesn't get re-evaluated as "always due" on every subsequent tick.
    assert runtime._replay_last_sampled_sim_time[recording["id"]] == 100.0


async def test_not_due_until_simulated_interval_elapses(client, database):
    """The core behavior this function exists for: no new sample until
    simulation time has actually advanced by the recording's own interval
    -- proves a recording can't be double-sampled for the same completed
    tick, or for a tick that hasn't advanced far enough yet."""
    device = _make_device(client, instance=9803, name="Local Sim Device 3")
    point = _make_point(database, device["id"], instance=0, name="Zone Temp")
    recording = database.create_replay_recording(device["id"], {
        "name": "Interval Test",
        "point_ids": [point["id"]],
        "sample_interval_seconds": 10.0,
        "maximum_samples": 10,
        "buffer_mode": "overwrite",
    })
    runtime._replay_last_sampled_sim_time.pop(recording["id"], None)

    engine = _StubEngine({point["id"]: 1.0}, elapsed_seconds=100.0)
    await runtime._sample_due_simulated_recordings(database, engine)
    assert database.get_replay_recording(recording["id"])["sample_count"] == 1

    # Only 5 simulated seconds later -- interval is 10s, so not due yet.
    engine.state.elapsed_seconds = 105.0
    engine._values[point["id"]] = 2.0
    await runtime._sample_due_simulated_recordings(database, engine)
    assert database.get_replay_recording(recording["id"])["sample_count"] == 1

    # Now 10 simulated seconds after the first sample -- due.
    engine.state.elapsed_seconds = 110.0
    engine._values[point["id"]] = 3.0
    await runtime._sample_due_simulated_recordings(database, engine)
    updated = database.get_replay_recording(recording["id"])
    assert updated["sample_count"] == 2
    samples = database.get_replay_recording_samples(recording["id"], 1)
    assert samples[0]["value"] == "3.0"


async def test_external_bacnet_device_is_not_sampled_here(database):
    """External devices are sampled on wall-clock time by
    _replay_recording_sample_once instead -- this function must ignore them
    entirely, never calling get_object_value for one."""
    device = database.sync_external_devices([{
        "device_instance": 9804,
        "name": "External Device",
        "host": "127.0.0.1",
        "port": 47808,
        "metadata": {},
    }])[0]
    point = database.create_object(device["id"], {
        "object_type": "analog-input", "object_instance": 0, "name": "Zone Temp",
        "units": "degrees-celsius", "behavior": "constant", "behavior_params": '{"value":0}',
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected",
        "polarity": "normal", "point_type": None,
    })
    recording = database.create_replay_recording(device["id"], {
        "name": "External Test",
        "point_ids": [point["id"]],
        "sample_interval_seconds": 5.0,
        "maximum_samples": 10,
        "buffer_mode": "overwrite",
    })
    runtime._replay_last_sampled_sim_time.pop(recording["id"], None)

    engine = _StubEngine({point["id"]: 21.5}, elapsed_seconds=100.0)
    await runtime._sample_due_simulated_recordings(database, engine)

    assert database.get_replay_recording(recording["id"])["sample_count"] == 0

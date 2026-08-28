"""SimEngine.advance_replay_playback -- basic playback stepping, driven
directly (unit level, not the full replay_playback_loop asyncio task).
Elapsed wall-clock time is simulated by rewinding the in-memory playback
state's last_advance_wall_time rather than actually sleeping."""
from __future__ import annotations

import asyncio

from src.simulation.engine import SimEngine


def _make_external_device(database, *, instance: int, name: str) -> dict:
    return database.sync_external_devices([{
        "device_instance": instance,
        "name": name,
        "host": "127.0.0.1",
        "port": 47808,
        "metadata": {},
    }])[0]


def _make_point(database, device_id: int, *, instance: int, name: str) -> dict:
    return database.create_object(device_id, {
        "object_type": "analog-input", "object_instance": instance, "name": name,
        "units": "degrees-celsius", "behavior": "constant", "behavior_params": '{"value":0}',
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected",
        "polarity": "normal", "point_type": None,
    })


def _make_replay_device(database, *, instance: int, recording_id: int) -> dict:
    return database.create_device({
        "device_instance": instance, "name": "Replay Copy", "description": "",
        "vendor_name": "Iotistica", "model_name": "BACnet Simulator", "enabled": True,
        "firmware_revision": "N/A", "protocol_revision": 22, "max_apdu_length_accepted": 1024,
        "segmentation_supported": "segmented-both", "simulation_mode": "replay",
        "replay_recording_id": recording_id,
    })


def _seed_three_sample_recording(database):
    source = _make_external_device(database, instance=9001, name="Playback Source")
    point = _make_point(database, source["id"], instance=0, name="Zone Temp")
    recording = database.create_replay_recording(source["id"], {
        "name": "Playback Test",
        "point_ids": [point["id"]],
        "sample_interval_seconds": 1.0,
        "maximum_samples": 10,
        "buffer_mode": "stop",
    })
    point_id = recording["points"][0]["id"]
    for value in (10, 20, 30):
        database.add_replay_sample(recording["id"], {point_id: {"value": value}})
    database.stop_replay_recording(recording["id"])
    return recording


def _rewind_last_advance(engine: SimEngine, device_id: int, seconds: float) -> None:
    state = engine._get_replay_state(device_id)
    state["last_advance_wall_time"] -= seconds


def test_play_advances_sample_index_in_order(client, database):
    recording = _seed_three_sample_recording(database)
    device = _make_replay_device(database, instance=9101, recording_id=recording["id"])
    engine = SimEngine(database)

    engine.replay_play(device["id"])
    _rewind_last_advance(engine, device["id"], 1.5)  # 1.5 * 1.0s interval -> one step
    asyncio.run(engine.advance_replay_playback(device))

    assert engine.get_replay_state(device["id"])["current_sample_index"] == 1


def test_natural_end_holds_last_sample_without_looping(client, database):
    recording = _seed_three_sample_recording(database)
    device = _make_replay_device(database, instance=9102, recording_id=recording["id"])
    engine = SimEngine(database)

    engine.replay_play(device["id"])
    _rewind_last_advance(engine, device["id"], 10.0)  # far past the end
    asyncio.run(engine.advance_replay_playback(device))

    state = engine.get_replay_state(device["id"])
    assert state["current_sample_index"] == 2  # last sample_index, held -- not reset to 0
    assert state["status"] == "stopped"


def test_loop_wraps_to_first_sample_and_keeps_playing(client, database):
    recording = _seed_three_sample_recording(database)
    device = _make_replay_device(database, instance=9103, recording_id=recording["id"])
    engine = SimEngine(database)

    engine.replay_play(device["id"])
    engine.replay_set_loop(device["id"], True)
    # 3 samples (indices 0,1,2) -> exactly 3 steps (0->1->2->wrap to 0) is
    # one full lap, landing precisely back on the first sample.
    _rewind_last_advance(engine, device["id"], 3.5)
    asyncio.run(engine.advance_replay_playback(device))

    state = engine.get_replay_state(device["id"])
    assert state["current_sample_index"] == 0
    assert state["status"] == "playing"


def test_pause_holds_current_position(client, database):
    recording = _seed_three_sample_recording(database)
    device = _make_replay_device(database, instance=9104, recording_id=recording["id"])
    engine = SimEngine(database)

    engine.replay_play(device["id"])
    _rewind_last_advance(engine, device["id"], 1.5)
    asyncio.run(engine.advance_replay_playback(device))
    assert engine.get_replay_state(device["id"])["current_sample_index"] == 1

    engine.replay_pause(device["id"])
    asyncio.run(engine.advance_replay_playback(device))

    state = engine.get_replay_state(device["id"])
    assert state["status"] == "paused"
    assert state["current_sample_index"] == 1


def test_explicit_stop_resets_to_first_sample(client, database):
    recording = _seed_three_sample_recording(database)
    device = _make_replay_device(database, instance=9105, recording_id=recording["id"])
    engine = SimEngine(database)

    engine.replay_play(device["id"])
    _rewind_last_advance(engine, device["id"], 1.5)
    asyncio.run(engine.advance_replay_playback(device))
    assert engine.get_replay_state(device["id"])["current_sample_index"] == 1

    asyncio.run(engine.replay_stop(device["id"]))

    state = engine.get_replay_state(device["id"])
    assert state["status"] == "stopped"
    assert state["current_sample_index"] == 0


def test_seek_while_paused_is_picked_up_on_next_advance(client, database):
    recording = _seed_three_sample_recording(database)
    device = _make_replay_device(database, instance=9106, recording_id=recording["id"])
    engine = SimEngine(database)

    engine.replay_seek(device["id"], 2)
    asyncio.run(engine.advance_replay_playback(device))

    state = engine.get_replay_state(device["id"])
    assert state["current_sample_index"] == 2
    assert state["status"] == "stopped"  # seek alone doesn't start playback

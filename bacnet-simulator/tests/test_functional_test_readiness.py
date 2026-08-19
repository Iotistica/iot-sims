"""Tests for pre-flight readiness (src/functional_tests/readiness.py) --
replaces the old semantic point_type resolver (resolution.py, deleted):
since every point reference in a saved definition is now a concrete
PointRef, there's nothing left to resolve at Run time, only to verify
exists (and, for Set nodes, is simulated)."""
from __future__ import annotations

from src.functional_tests.readiness import build_point_cache, check_readiness, collect_point_refs


def _make_device(database, name="AHU-1", instance=9001, source_type="simulated"):
    with database._conn() as conn:
        conn.execute(
            "INSERT INTO devices (device_instance, name, source_type) VALUES (?,?,?)",
            (instance, name, source_type),
        )
        conn.commit()
        return conn.execute("SELECT id FROM devices WHERE device_instance=?", (instance,)).fetchone()[0]


def _make_object(database, device_id, name, object_instance, object_type="binary-output"):
    with database._conn() as conn:
        conn.execute(
            "INSERT INTO objects (device_id, object_type, object_instance, name) VALUES (?,?,?,?)",
            (device_id, object_type, object_instance, name),
        )
        conn.commit()
        return conn.execute(
            "SELECT id FROM objects WHERE device_id=? AND name=?", (device_id, name)
        ).fetchone()[0]


def _definition_with_all_point_kinds(device_id, object_id):
    point = {"device_id": device_id, "object_id": object_id}
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "cap", "type": "capture", "params": {"point": point, "variable": "x"}},
            {"id": "wu", "type": "wait_until", "params": {
                "point": point, "operator": "eq",
                "value": {"kind": "point", "point": point},
                "timeout_seconds": 60,
            }},
            {"id": "set", "type": "set", "params": {"point": point, "value": "OFF"}},
            {"id": "v", "type": "verify", "params": {
                "left": {"kind": "point", "point": point},
                "operator": "eq",
                "right": {"kind": "constant", "value": 1},
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        "edges": [],
    }


def test_collect_point_refs_walks_every_point_bearing_node_type(database):
    device_id = _make_device(database)
    object_id = _make_object(database, device_id, "CH-2-Run", 1)

    refs = collect_point_refs(_definition_with_all_point_kinds(device_id, object_id))

    # All five node types reference the SAME (device_id, object_id) --
    # dedup must collapse them to one entry.
    assert len(refs) == 1
    assert refs[0]["device_id"] == device_id
    assert refs[0]["object_id"] == object_id
    assert refs[0]["used_by_set"] is True


def test_collect_point_refs_only_flags_used_by_set_for_the_set_node(database):
    device_id = _make_device(database)
    object_a = _make_object(database, device_id, "A", 1)
    object_b = _make_object(database, device_id, "B", 2)

    definition = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "cap", "type": "capture", "params": {
                "point": {"device_id": device_id, "object_id": object_a}, "variable": "x",
            }},
            {"id": "set", "type": "set", "params": {
                "point": {"device_id": device_id, "object_id": object_b}, "value": "OFF",
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        "edges": [],
    }

    refs = {(r["device_id"], r["object_id"]): r for r in collect_point_refs(definition)}
    assert refs[(device_id, object_a)]["used_by_set"] is False
    assert refs[(device_id, object_b)]["used_by_set"] is True


def test_check_readiness_ok_for_an_existing_simulated_point(database):
    device_id = _make_device(database)
    object_id = _make_object(database, device_id, "CH-2-Run", 1)

    definition = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "cap", "type": "capture", "params": {
                "point": {"device_id": device_id, "object_id": object_id}, "variable": "x",
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        "edges": [],
    }

    results = check_readiness(database, definition)
    assert len(results) == 1
    assert results[0].status == "ok"
    assert results[0].device_name == "AHU-1"
    assert results[0].object_name == "CH-2-Run"


def test_check_readiness_flags_missing_device(database):
    definition = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "cap", "type": "capture", "params": {
                "point": {"device_id": 999999, "object_id": 1}, "variable": "x",
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        "edges": [],
    }
    results = check_readiness(database, definition)
    assert results[0].status == "missing_device"


def test_check_readiness_flags_missing_object(database):
    device_id = _make_device(database)
    definition = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "cap", "type": "capture", "params": {
                "point": {"device_id": device_id, "object_id": 999999}, "variable": "x",
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        "edges": [],
    }
    results = check_readiness(database, definition)
    assert results[0].status == "missing_object"


def test_check_readiness_flags_set_node_targeting_external_device(database):
    device_id = _make_device(database, name="External-AHU", instance=9002, source_type="external-bacnet")
    object_id = _make_object(database, device_id, "CH-2-Run", 1)

    definition = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "set", "type": "set", "params": {
                "point": {"device_id": device_id, "object_id": object_id}, "value": "OFF",
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        "edges": [],
    }
    results = check_readiness(database, definition)
    assert results[0].status == "not_simulated"


def test_check_readiness_allows_reading_an_external_device_point_when_not_used_by_set(database):
    device_id = _make_device(database, name="External-AHU", instance=9003, source_type="external-bacnet")
    object_id = _make_object(database, device_id, "SAT", 1, object_type="analog-input")

    definition = {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "cap", "type": "capture", "params": {
                "point": {"device_id": device_id, "object_id": object_id}, "variable": "sat",
            }},
            {"id": "end", "type": "end", "params": {"result": "pass"}},
        ],
        "edges": [],
    }
    results = check_readiness(database, definition)
    assert results[0].status == "ok"


def test_build_point_cache_returns_one_entry_per_distinct_point(database):
    device_id = _make_device(database)
    object_id = _make_object(database, device_id, "CH-2-Run", 1)

    point_refs = [{"device_id": device_id, "object_id": object_id, "used_by_set": True}]
    cache = build_point_cache(database, point_refs)

    assert set(cache.keys()) == {(device_id, object_id)}
    assert cache[(device_id, object_id)]["object"]["name"] == "CH-2-Run"
    assert cache[(device_id, object_id)]["device"]["name"] == "AHU-1"


def test_build_point_cache_skips_unresolvable_refs(database):
    point_refs = [{"device_id": 999999, "object_id": 1, "used_by_set": False}]
    cache = build_point_cache(database, point_refs)
    assert cache == {}

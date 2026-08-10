"""Functional Tests are project-level data (per review: not deferred from
Save/Open/New Project) -- these tests are the explicitly requested
acceptance case: create a Functional Test, save the project, wipe live
state, reload the project, and confirm the test's definition (nodes/edges/
params) and layout come back identical. Also verifies New Project
(clear_live_state) removes functional tests from live state."""
from __future__ import annotations


def _definition():
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "start", "params": {}},
            {"id": "wait-run", "type": "wait_until", "params": {
                "point_type": "Run_Status", "operator": "eq", "value": True, "timeout_seconds": 600,
            }},
            {"id": "capture-lwt", "type": "capture", "params": {
                "point_type": "Leaving_Hot_Water_Temperature_Sensor", "variable": "lwt",
            }},
            {"id": "verify-lwt", "type": "verify", "params": {
                "left": {"kind": "variable", "name": "lwt"},
                "operator": "gt",
                "right": {"kind": "constant", "value": 60},
            }},
            {"id": "end-pass", "type": "end", "params": {"result": "pass"}},
            {"id": "end-fail", "type": "end", "params": {"result": "fail", "message": "LWT too low"}},
        ],
        "edges": [
            {"source": "start", "target": "wait-run", "source_handle": None},
            {"source": "wait-run", "target": "capture-lwt", "source_handle": None},
            {"source": "capture-lwt", "target": "verify-lwt", "source_handle": None},
            {"source": "verify-lwt", "target": "end-pass", "source_handle": "pass"},
            {"source": "verify-lwt", "target": "end-fail", "source_handle": "fail"},
        ],
        "layout": {
            "start": {"x": 250, "y": 40},
            "wait-run": {"x": 220, "y": 160},
            "capture-lwt": {"x": 220, "y": 280},
            "verify-lwt": {"x": 220, "y": 400},
            "end-pass": {"x": 120, "y": 520},
            "end-fail": {"x": 320, "y": 520},
        },
    }


def test_functional_test_survives_project_save_and_reload(database):
    definition = _definition()
    created = database.create_functional_test({
        "name": "Boiler Heating Response",
        "description": "Confirms LWT rises after boiler run status goes true",
        "equipment_type": "Boiler",
        "definition": definition,
    })

    project = database.save_project("Round-trip Test", "")

    # New Project: live state wiped, the test is gone.
    database.clear_live_state()
    assert database.get_functional_tests() == []

    # Open Project: the test comes back with an identical definition.
    database.load_project(project["id"])

    reloaded = database.get_functional_tests()
    assert len(reloaded) == 1
    assert reloaded[0]["name"] == created["name"]
    assert reloaded[0]["equipment_type"] == created["equipment_type"]
    assert reloaded[0]["definition"]["nodes"] == definition["nodes"]
    assert reloaded[0]["definition"]["edges"] == definition["edges"]
    assert reloaded[0]["definition"]["layout"] == definition["layout"]


def test_functional_tests_isolated_between_projects(database):
    database.create_functional_test({
        "name": "Boiler Heating Response",
        "description": "",
        "equipment_type": "Boiler",
        "definition": _definition(),
    })
    project_a = database.save_project("Project A", "")

    # Switching to a second, unrelated project must not carry the first
    # project's functional tests along.
    database.clear_live_state()
    project_b = database.save_project("Project B", "")
    assert database.get_functional_tests() == []

    database.load_project(project_b["id"])
    assert database.get_functional_tests() == []

    database.load_project(project_a["id"])
    reloaded = database.get_functional_tests()
    assert len(reloaded) == 1
    assert reloaded[0]["name"] == "Boiler Heating Response"


def test_clear_live_state_removes_functional_tests(seeded_database):
    seeded_database.create_functional_test({
        "name": "Boiler Heating Response",
        "description": "",
        "equipment_type": "Boiler",
        "definition": _definition(),
    })
    assert seeded_database.get_functional_tests()

    seeded_database.clear_live_state()

    assert seeded_database.get_functional_tests() == []


def test_no_duplicate_functional_tests_on_repeated_reload(database):
    database.create_functional_test({
        "name": "Boiler Heating Response",
        "description": "",
        "equipment_type": "Boiler",
        "definition": _definition(),
    })
    project = database.save_project("Round-trip Test", "")

    database.load_project(project["id"])
    database.load_project(project["id"])
    database.load_project(project["id"])

    assert len(database.get_functional_tests()) == 1

"""Custom Graphs are project-level data, same as Functional Tests -- these
tests mirror test_functional_tests_project_roundtrip.py exactly: create a
saved graph, save the project, wipe live state, reload the project, and
confirm the definition (series/axes/colors/visibility) comes back
identical. Also verifies New Project (clear_live_state) removes saved
graphs from live state."""
from __future__ import annotations


def _definition():
    return {
        "version": 1,
        "series": [
            {"device_id": 1, "object_id": 10, "color": "#1890ff", "axis": "left", "visible": True},
            {"device_id": 2, "object_id": 20, "color": "#52c41a", "axis": "right", "visible": False},
        ],
        "time_range": "live",
    }


def test_custom_graph_survives_project_save_and_reload(database):
    definition = _definition()
    created = database.create_custom_graph({
        "name": "RTU vs. Zone Temps",
        "definition": definition,
    })

    project = database.save_project("Round-trip Test", "")

    # New Project: live state wiped, the graph is gone.
    database.clear_live_state()
    assert database.get_custom_graphs() == []

    # Open Project: the graph comes back with an identical definition.
    database.load_project(project["id"])

    reloaded = database.get_custom_graphs()
    assert len(reloaded) == 1
    assert reloaded[0]["name"] == created["name"]
    assert reloaded[0]["definition"] == definition


def test_custom_graphs_isolated_between_projects(database):
    database.create_custom_graph({
        "name": "RTU vs. Zone Temps",
        "definition": _definition(),
    })
    project_a = database.save_project("Project A", "")

    # Switching to a second, unrelated project must not carry the first
    # project's saved graphs along.
    database.clear_live_state()
    project_b = database.save_project("Project B", "")
    assert database.get_custom_graphs() == []

    database.load_project(project_b["id"])
    assert database.get_custom_graphs() == []

    database.load_project(project_a["id"])
    reloaded = database.get_custom_graphs()
    assert len(reloaded) == 1
    assert reloaded[0]["name"] == "RTU vs. Zone Temps"


def test_clear_live_state_removes_custom_graphs(seeded_database):
    seeded_database.create_custom_graph({
        "name": "RTU vs. Zone Temps",
        "definition": _definition(),
    })
    assert seeded_database.get_custom_graphs()

    seeded_database.clear_live_state()

    assert seeded_database.get_custom_graphs() == []


def test_dashboard_grouping_and_grid_position_survive_project_reload(database):
    """A widget's dashboard_id is a real FK and DOES need id-remapping on
    reload (unlike the rest of a custom_graphs row) -- confirm a widget
    ends up back on a dashboard with the SAME NAME (the id itself is
    expected to change) and its grid position is preserved."""
    dash_a = database.create_dashboard("Ops View")
    dash_b = database.create_dashboard("Energy View")
    database.create_custom_graph({
        "name": "On Ops", "definition": _definition(), "dashboard_id": dash_a["id"],
        "grid_x": 2, "grid_y": 4,
    })
    database.create_custom_graph({
        "name": "On Energy", "definition": _definition(), "dashboard_id": dash_b["id"],
    })

    project = database.save_project("Round-trip Test", "")
    database.clear_live_state()
    assert database.get_dashboards() == []

    database.load_project(project["id"])

    dashboards_by_name = {d["name"]: d for d in database.get_dashboards()}
    assert set(dashboards_by_name) == {"Ops View", "Energy View"}

    reloaded = {g["name"]: g for g in database.get_custom_graphs()}
    assert reloaded["On Ops"]["dashboard_id"] == dashboards_by_name["Ops View"]["id"]
    assert (reloaded["On Ops"]["grid_x"], reloaded["On Ops"]["grid_y"]) == (2, 4)
    assert reloaded["On Energy"]["dashboard_id"] == dashboards_by_name["Energy View"]["id"]


def test_no_duplicate_custom_graphs_on_repeated_reload(database):
    database.create_custom_graph({
        "name": "RTU vs. Zone Temps",
        "definition": _definition(),
    })
    project = database.save_project("Round-trip Test", "")

    database.load_project(project["id"])
    database.load_project(project["id"])
    database.load_project(project["id"])

    assert len(database.get_custom_graphs()) == 1

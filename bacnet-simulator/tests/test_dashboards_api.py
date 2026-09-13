"""API-level tests for Dashboards (src/api/routers/dashboards.py) and the
dashboard/widget fields migration 28 added to custom_graphs (dashboard_id,
widget_type, grid_x/y/w/h) -- see registry.py's _migration_028_dashboards
docstring for why widgets live on the existing custom_graphs table rather
than a new one. Mirrors test_custom_graphs_api.py's structure.
"""
from __future__ import annotations

import pytest


def _chart_definition(**overrides):
    definition = {
        "version": 1,
        "series": [
            {"device_id": 1, "object_id": 10, "color": "#1890ff", "axis": "left", "visible": True},
        ],
        "time_range": "live",
    }
    definition.update(overrides)
    return definition


def _kpi_definition(**overrides):
    definition = {"device_id": 1, "object_id": 10}
    definition.update(overrides)
    return definition


def _table_definition(**overrides):
    definition = {"device_id": 1, "object_id": 10}
    definition.update(overrides)
    return definition


def test_create_get_update_delete_dashboard(client):
    resp = client.post("/dashboards", json={"name": "Ops View"})
    assert resp.status_code == 201, resp.text
    dashboard = resp.json()
    assert dashboard["name"] == "Ops View"
    dashboard_id = dashboard["id"]

    resp = client.get(f"/dashboards/{dashboard_id}")
    assert resp.status_code == 200
    assert resp.json()["name"] == "Ops View"

    resp = client.get("/dashboards")
    assert resp.status_code == 200
    assert len(resp.json()) == 1

    resp = client.put(f"/dashboards/{dashboard_id}", json={"name": "Ops View (Renamed)"})
    assert resp.status_code == 200
    assert resp.json()["name"] == "Ops View (Renamed)"

    resp = client.delete(f"/dashboards/{dashboard_id}")
    assert resp.status_code == 204

    resp = client.get(f"/dashboards/{dashboard_id}")
    assert resp.status_code == 404


def test_get_update_delete_missing_dashboard_404(client):
    assert client.get("/dashboards/999999").status_code == 404
    assert client.put("/dashboards/999999", json={"name": "x"}).status_code == 404
    assert client.delete("/dashboards/999999").status_code == 404


def test_deleting_a_dashboard_cascades_to_its_widgets(client):
    dashboard = client.post("/dashboards", json={"name": "Ops View"}).json()
    widget = client.post("/custom-graphs", json={
        "name": "Zone Temps", "definition": _chart_definition(), "dashboard_id": dashboard["id"],
    }).json()

    resp = client.delete(f"/dashboards/{dashboard['id']}")
    assert resp.status_code == 204

    assert client.get(f"/custom-graphs/{widget['id']}").status_code == 404


def test_widget_grid_and_type_fields_round_trip(client):
    dashboard = client.post("/dashboards", json={"name": "Ops View"}).json()
    resp = client.post("/custom-graphs", json={
        "name": "Zone Temps",
        "definition": _chart_definition(),
        "dashboard_id": dashboard["id"],
        "widget_type": "chart",
        "grid_x": 2, "grid_y": 4, "grid_w": 8, "grid_h": 5,
    })
    assert resp.status_code == 201, resp.text
    widget = resp.json()
    assert widget["dashboard_id"] == dashboard["id"]
    assert widget["widget_type"] == "chart"
    assert (widget["grid_x"], widget["grid_y"], widget["grid_w"], widget["grid_h"]) == (2, 4, 8, 5)

    resp = client.put(f"/custom-graphs/{widget['id']}", json={
        "name": "Zone Temps", "definition": _chart_definition(), "dashboard_id": dashboard["id"],
        "grid_x": 6, "grid_y": 0,
    })
    assert resp.status_code == 200
    assert (resp.json()["grid_x"], resp.json()["grid_y"]) == (6, 0)


def test_update_omitting_grid_and_type_fields_preserves_them(client):
    """SavedGraphCard.vue's persist() saves content edits (rename, add/
    remove a point, toggle axis/visible/role) WITHOUT knowing or resending
    grid geometry -- that must never reset position/size/type back to
    defaults, only an explicit new value should ever change them."""
    dashboard = client.post("/dashboards", json={"name": "Ops View"}).json()
    widget = client.post("/custom-graphs", json={
        "name": "Zone Temps", "definition": _chart_definition(), "dashboard_id": dashboard["id"],
        "widget_type": "chart", "grid_x": 2, "grid_y": 4, "grid_w": 8, "grid_h": 5,
    }).json()

    resp = client.put(f"/custom-graphs/{widget['id']}", json={
        "name": "Zone Temps (Renamed)", "definition": _chart_definition(),
    })
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["name"] == "Zone Temps (Renamed)"
    assert updated["dashboard_id"] == dashboard["id"]
    assert updated["widget_type"] == "chart"
    assert (updated["grid_x"], updated["grid_y"], updated["grid_w"], updated["grid_h"]) == (2, 4, 8, 5)


def test_update_with_only_grid_fields_preserves_definition_and_type(client):
    """The inverse: a pure layout-drag save (grid_x/y change only) must not
    touch the widget's content or type."""
    dashboard = client.post("/dashboards", json={"name": "Ops View"}).json()
    widget = client.post("/custom-graphs", json={
        "name": "Supply Air Temp", "definition": _kpi_definition(), "dashboard_id": dashboard["id"],
        "widget_type": "kpi",
    }).json()

    resp = client.put(f"/custom-graphs/{widget['id']}", json={
        "name": "Supply Air Temp", "definition": _kpi_definition(), "grid_x": 6, "grid_y": 1,
    })
    assert resp.status_code == 200, resp.text
    updated = resp.json()
    assert updated["widget_type"] == "kpi"
    assert updated["definition"] == _kpi_definition()
    assert (updated["grid_x"], updated["grid_y"]) == (6, 1)


def test_widget_with_no_dashboard_id_lands_on_a_default_dashboard(client):
    """ObjectsPanel.vue's quick single-point graph icon has no
    dashboard-switcher context -- omitting dashboard_id must still work and
    land the widget somewhere, not 422/500."""
    resp = client.post("/custom-graphs", json={"name": "Quick Graph", "definition": _chart_definition()})
    assert resp.status_code == 201, resp.text
    widget = resp.json()
    assert widget["dashboard_id"] is not None

    # A second such widget lands on the SAME default dashboard, not a fresh
    # one each time.
    resp2 = client.post("/custom-graphs", json={"name": "Quick Graph 2", "definition": _chart_definition()})
    assert resp2.json()["dashboard_id"] == widget["dashboard_id"]


def test_dashboard_filters_custom_graphs_list(client):
    dash_a = client.post("/dashboards", json={"name": "A"}).json()
    dash_b = client.post("/dashboards", json={"name": "B"}).json()
    client.post("/custom-graphs", json={"name": "On A", "definition": _chart_definition(), "dashboard_id": dash_a["id"]})
    client.post("/custom-graphs", json={"name": "On B", "definition": _chart_definition(), "dashboard_id": dash_b["id"]})

    resp = client.get(f"/custom-graphs?dashboard_id={dash_a['id']}")
    assert resp.status_code == 200
    names = {g["name"] for g in resp.json()}
    assert names == {"On A"}


def test_create_kpi_widget(client):
    dashboard = client.post("/dashboards", json={"name": "Ops View"}).json()
    resp = client.post("/custom-graphs", json={
        "name": "Supply Air Temp",
        "definition": _kpi_definition(),
        "dashboard_id": dashboard["id"],
        "widget_type": "kpi",
    })
    assert resp.status_code == 201, resp.text
    widget = resp.json()
    assert widget["widget_type"] == "kpi"
    assert widget["definition"] == _kpi_definition()


@pytest.mark.parametrize(
    "mutate,description",
    [
        (lambda d: d.pop("device_id"), "missing device_id"),
        (lambda d: d.__setitem__("device_id", "1"), "device_id not an integer"),
        (lambda d: d.pop("object_id"), "missing object_id"),
        (lambda d: d.__setitem__("decimals", "2"), "decimals not an integer"),
    ],
)
def test_invalid_kpi_definition_rejected(client, mutate, description):
    definition = _kpi_definition()
    mutate(definition)
    resp = client.post("/custom-graphs", json={
        "name": "Bad KPI", "definition": definition, "widget_type": "kpi",
    })
    assert resp.status_code == 400, f"{description}: expected 400, got {resp.status_code}: {resp.text}"


def test_create_table_widget(client):
    dashboard = client.post("/dashboards", json={"name": "Ops View"}).json()
    resp = client.post("/custom-graphs", json={
        "name": "Zone Temp History",
        "definition": _table_definition(columns={"min": True, "max": True}, page_size=25),
        "dashboard_id": dashboard["id"],
        "widget_type": "table",
    })
    assert resp.status_code == 201, resp.text
    widget = resp.json()
    assert widget["widget_type"] == "table"
    assert widget["definition"] == _table_definition(columns={"min": True, "max": True}, page_size=25)


@pytest.mark.parametrize(
    "mutate,description",
    [
        (lambda d: d.pop("device_id"), "missing device_id"),
        (lambda d: d.pop("object_id"), "missing object_id"),
        (lambda d: d.__setitem__("columns", "yes"), "columns not an object"),
        (lambda d: d.__setitem__("columns", {"quality": True}), "unknown column key"),
        (lambda d: d.__setitem__("columns", {"min": "yes"}), "column value not a boolean"),
        (lambda d: d.__setitem__("page_size", 0), "page_size not positive"),
        (lambda d: d.__setitem__("page_size", "10"), "page_size not an integer"),
    ],
)
def test_invalid_table_definition_rejected(client, mutate, description):
    definition = _table_definition()
    mutate(definition)
    resp = client.post("/custom-graphs", json={
        "name": "Bad Table", "definition": definition, "widget_type": "table",
    })
    assert resp.status_code == 400, f"{description}: expected 400, got {resp.status_code}: {resp.text}"


def test_table_widget_update_omitting_type_preserves_it(client):
    """Same 'omitted means unchanged' guarantee already proven for kpi
    widgets (see test_update_omitting_grid_and_type_fields_preserves_them)
    -- confirm it also holds for the newer table type."""
    widget = client.post("/custom-graphs", json={
        "name": "Zone Temp History", "definition": _table_definition(), "widget_type": "table",
    }).json()

    resp = client.put(f"/custom-graphs/{widget['id']}", json={
        "name": "Zone Temp History (Renamed)", "definition": _table_definition(),
    })
    assert resp.status_code == 200, resp.text
    assert resp.json()["widget_type"] == "table"


def test_invalid_widget_type_rejected(client):
    resp = client.post("/custom-graphs", json={
        "name": "Bad Type", "definition": _chart_definition(), "widget_type": "gauge",
    })
    assert resp.status_code == 422


def test_series_role_defaults_to_pv_and_accepts_setpoint(client):
    definition = _chart_definition(series=[
        {"device_id": 1, "object_id": 10, "color": "#1890ff", "axis": "left", "visible": True},
        {"device_id": 1, "object_id": 11, "color": "#faad14", "axis": "left", "visible": True, "role": "setpoint"},
    ])
    resp = client.post("/custom-graphs", json={"name": "PV + Setpoint", "definition": definition})
    assert resp.status_code == 201, resp.text
    series = resp.json()["definition"]["series"]
    assert series[0].get("role", "pv") == "pv"
    assert series[1]["role"] == "setpoint"


def test_invalid_series_role_rejected(client):
    definition = _chart_definition()
    definition["series"][0]["role"] = "target"
    resp = client.post("/custom-graphs", json={"name": "Bad Role", "definition": definition})
    assert resp.status_code == 400

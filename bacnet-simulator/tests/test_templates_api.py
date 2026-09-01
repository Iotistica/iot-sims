"""HTTP-level tests for /templates -- the object-template feature moved from
client-only storage (hardcoded array + localStorage) into SQLite this
session. Covers: migration 22 seeds exactly the 8 built-ins as delete-
protected rows; GET returns them; POST creates a user row that round-trips;
DELETE succeeds for a user row and is refused (400) for a built-in one."""
from __future__ import annotations


def test_migration_seeds_eight_builtin_templates(database):
    with database._conn() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM templates WHERE is_builtin = 1")]
    assert len(rows) == 8
    keys = {r["key"] for r in rows}
    assert keys == {"ahu", "vav", "fcu", "chiller", "boiler", "bms", "meter", "lighting"}


def test_list_templates_returns_builtins(client):
    resp = client.get("/templates")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 8
    assert all(t["is_builtin"] for t in body)
    ahu = next(t for t in body if t["key"] == "ahu")
    assert ahu["label"] == "Air Handling Unit"
    assert "Rooftop_Unit" in ahu["equipment_types"]
    assert len(ahu["objects"]) > 0


def test_create_user_template_round_trips(client):
    resp = client.post("/templates", json={
        "label": "My Custom Template",
        "description": "A test template",
        "objects": [{
            "object_type": "analog-input",
            "object_instance": 1,
            "name": "TEMP1",
            "units": "degrees-celsius",
            "behavior": "static",
            "behavior_params": "{}",
        }],
        "equipment_types": ["Air_Handling_Unit"],
    })
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["is_builtin"] is False
    assert created["label"] == "My Custom Template"
    assert created["equipment_types"] == ["Air_Handling_Unit"]
    assert len(created["objects"]) == 1

    listed = client.get("/templates").json()
    assert len(listed) == 9
    match = next(t for t in listed if t["id"] == created["id"])
    assert match["label"] == "My Custom Template"


def test_create_template_rejects_unknown_equipment_type(client):
    resp = client.post("/templates", json={
        "label": "Bad Template",
        "description": "",
        "objects": [{
            "object_type": "analog-input",
            "object_instance": 1,
            "name": "X",
            "units": "no-units",
            "behavior": "static",
            "behavior_params": "{}",
        }],
        "equipment_types": ["Not_A_Real_Type"],
    })
    assert resp.status_code == 400


def test_delete_user_template_succeeds(client):
    created = client.post("/templates", json={
        "label": "Deletable",
        "description": "",
        "objects": [{
            "object_type": "analog-input",
            "object_instance": 1,
            "name": "X",
            "units": "no-units",
            "behavior": "static",
            "behavior_params": "{}",
        }],
    }).json()

    resp = client.delete(f"/templates/{created['id']}")
    assert resp.status_code == 204

    listed = client.get("/templates").json()
    assert all(t["id"] != created["id"] for t in listed)


def test_delete_builtin_template_is_refused(client):
    builtin = next(t for t in client.get("/templates").json() if t["is_builtin"])

    resp = client.delete(f"/templates/{builtin['id']}")
    assert resp.status_code == 400

    listed = client.get("/templates").json()
    assert any(t["id"] == builtin["id"] for t in listed)


def test_delete_unknown_template_404s(client):
    resp = client.delete("/templates/999999")
    assert resp.status_code == 404

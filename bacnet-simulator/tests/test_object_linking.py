"""Per-object live-value linking (objects.source_object_id) -- the per-object
sibling of whole-device Mirror (devices.source_device_id/simulation_mode=
'mirror'). Covers the DB layer (database.create_linked_object/
get_linked_objects/get_link_consumers) and the new API guards: a link's
target device must stay in ordinary 'simulation' mode, a link's source must
not itself already be linked (the cycle-prevention rule), and a still-
linked-to point/device can't be deleted out from under its consumer.

Object creation in this file goes through database.create_object() directly,
not POST /devices/{id}/objects -- this Windows dev environment can't import
objects_router at all (external_objects.py -> discovery.py -> `import fcntl`,
POSIX-only, raised inside the SAME try/except conftest.py wraps objects_router
in, so neither router registers here; see existing session convention). For
the same reason, objects.py's own route handlers (link_object, delete_object)
are called directly as plain async functions with a minimal fake Request
(exposing only .app, which is the real TestClient app -- get_database/
get_engine/log_event only ever read request.app.state) instead of through
client.post/delete. Device-level routes (PUT/DELETE /devices/{id}) are NOT
affected -- devices_router imports fine -- so those stay real HTTP calls.

Sync-loop (_link_sync_once) and tick-loop dispatch are not covered here --
no existing test file exercises mirror_sync_loop/_mirror_sync_once either
(nothing to mirror), and both are small, easily-reviewed diffs on
already-tested paths (get_object_value, inject_mirror_values,
normalize_present_value); verified manually per the plan instead.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.api.routers import objects as objects_router_module
from src.bacnet.schemas import ObjectLinkCreate


def _fake_request(client):
    """Minimal stand-in for FastAPI's Request -- objects.py's route
    handlers only ever read request.app.state (get_database/get_engine/
    log_event), never anything else off it."""
    return SimpleNamespace(app=client.app)


def _make_object(database, device_id: int, *, object_instance: int = 1, name: str = "Zone-Temp") -> dict:
    return database.create_object(device_id, {
        "object_type": "analog-input",
        "object_instance": object_instance,
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


def _make_device_and_object(client, *, instance: int, name: str = "AHU-Test", object_instance: int = 1):
    device = client.post("/devices", json={"protocol": "bacnet", "device_instance": instance, "name": name}).json()
    obj = _make_object(client.app.state.db, device["id"], object_instance=object_instance)
    return device, obj


# ─── DB layer ────────────────────────────────────────────────────────────────

def test_create_linked_object_copies_identity_and_sets_source(client, database):
    source_dev, source_obj = _make_device_and_object(client, instance=4101, name="Source-Dev")
    target_dev = client.post("/devices", json={"protocol": "bacnet", "device_instance": 4102, "name": "Virtual-Dev"}).json()

    linked = database.create_linked_object(target_dev["id"], source_obj["id"])

    assert linked["device_id"] == target_dev["id"]
    assert linked["source_object_id"] == source_obj["id"]
    assert linked["object_type"] == source_obj["object_type"]
    assert linked["name"] == f"{source_obj['name']} (Source-Dev)"
    assert linked["units"] == source_obj["units"]
    assert linked["object_instance"] == 0  # first object of this type on an empty device


def test_create_linked_object_picks_free_instance(client, database):
    source_dev, source_obj = _make_device_and_object(client, instance=4103, name="Source-Dev-2")
    target_dev, existing_obj = _make_device_and_object(client, instance=4104, name="Target-Dev", object_instance=5)

    linked = database.create_linked_object(target_dev["id"], source_obj["id"])

    assert linked["object_instance"] == 6  # max(existing)+1 for this object_type on target_dev


def test_create_linked_object_names_after_source_device(client, database):
    """A linked object's name always carries its source device's name in
    brackets -- not just to avoid a collision, but so where it comes from
    is visible at a glance in the Name column itself, not only in the
    "Linked from..." tooltip. This also happens to be what avoids the
    BACnet unique-object-name-per-device collision (bacpypes3's
    Application.add_object enforces this independently of the objects
    table's own (device_id, object_type, object_instance) uniqueness) that
    copying the source's name verbatim used to hit the moment a point was
    linked onto the SAME device it came from -- confirmed live, the hot-add
    silently failing since it's a fire-and-forget task, leaving the DB row
    created but never actually registered with the running BACnet app, so
    no sync value ever reached it."""
    source_dev, source_obj = _make_device_and_object(client, instance=4120, name="Self-Link-Dev")

    linked = database.create_linked_object(source_dev["id"], source_obj["id"])

    assert linked["name"] == f"{source_obj['name']} (Self-Link-Dev)"


def test_create_linked_object_disambiguates_when_that_name_also_collides(client, database):
    source_dev, source_obj = _make_device_and_object(client, instance=4121, name="Source-Dev-Name")
    target_dev = client.post("/devices", json={"protocol": "bacnet", "device_instance": 4122, "name": "Busy-Target"}).json()
    # Pre-seed a name collision with what create_linked_object would
    # otherwise produce, forcing the numeric-suffix fallback.
    _make_object(database, target_dev["id"], object_instance=9, name=f"{source_obj['name']} (Source-Dev-Name)")

    linked = database.create_linked_object(target_dev["id"], source_obj["id"])

    assert linked["name"] == f"{source_obj['name']} (Source-Dev-Name) 2"


def test_get_linked_objects_joins_source_device(client, database):
    source_dev, source_obj = _make_device_and_object(client, instance=4105, name="Source-Dev-3")
    target_dev = client.post("/devices", json={"protocol": "bacnet", "device_instance": 4106, "name": "Virtual-Dev-2"}).json()
    linked = database.create_linked_object(target_dev["id"], source_obj["id"])

    rows = database.get_linked_objects()
    row = next(r for r in rows if r["id"] == linked["id"])
    assert row["source_object_id"] == source_obj["id"]
    assert row["source_device_id"] == source_dev["id"]
    assert row["source_object_type"] == source_obj["object_type"]
    assert row["source_object_instance"] == source_obj["object_instance"]


def test_get_link_consumers_lists_dependents(client, database):
    source_dev, source_obj = _make_device_and_object(client, instance=4107, name="Source-Dev-4")
    target_dev = client.post("/devices", json={"protocol": "bacnet", "device_instance": 4108, "name": "Virtual-Dev-3"}).json()
    linked = database.create_linked_object(target_dev["id"], source_obj["id"])

    consumers = database.get_link_consumers(source_obj["id"])
    assert len(consumers) == 1
    assert consumers[0]["id"] == linked["id"]
    assert consumers[0]["device_name"] == "Virtual-Dev-3"


def test_get_link_consumers_empty_for_unlinked_point(client, database):
    _dev, obj = _make_device_and_object(client, instance=4109, name="Lonely-Dev")
    assert database.get_link_consumers(obj["id"]) == []


# ─── API guards (objects_router handlers called directly -- see module docstring) ──

async def test_link_endpoint_creates_object(client):
    source_dev, source_obj = _make_device_and_object(client, instance=4201, name="Source-A")
    target_dev = client.post("/devices", json={"protocol": "bacnet", "device_instance": 4202, "name": "Virtual-A"}).json()

    result = await objects_router_module.link_object(
        target_dev["id"], ObjectLinkCreate(source_object_id=source_obj["id"]), _fake_request(client),
    )
    assert result["source_object_id"] == source_obj["id"]
    assert result["device_id"] == target_dev["id"]


async def test_link_rejects_target_device_not_in_simulation_mode(client):
    source_dev, source_obj = _make_device_and_object(client, instance=4203, name="Source-B")
    target_dev = client.post("/devices", json={
        "protocol": "bacnet", "device_instance": 4204, "name": "Mirror-Dev",
        "simulation_mode": "mirror", "source_device_id": source_dev["id"],
    }).json()

    with pytest.raises(Exception) as exc_info:
        await objects_router_module.link_object(
            target_dev["id"], ObjectLinkCreate(source_object_id=source_obj["id"]), _fake_request(client),
        )
    assert getattr(exc_info.value, "status_code", None) == 409


async def test_link_rejects_linking_to_an_already_linked_object(client, database):
    source_dev, source_obj = _make_device_and_object(client, instance=4205, name="Source-C")
    mid_dev = client.post("/devices", json={"protocol": "bacnet", "device_instance": 4206, "name": "Mid-Dev"}).json()
    mid_link = database.create_linked_object(mid_dev["id"], source_obj["id"])

    far_dev = client.post("/devices", json={"protocol": "bacnet", "device_instance": 4207, "name": "Far-Dev"}).json()
    with pytest.raises(Exception) as exc_info:
        await objects_router_module.link_object(
            far_dev["id"], ObjectLinkCreate(source_object_id=mid_link["id"]), _fake_request(client),
        )
    assert getattr(exc_info.value, "status_code", None) == 409


async def test_link_rejects_unknown_source(client):
    target_dev = client.post("/devices", json={"protocol": "bacnet", "device_instance": 4208, "name": "Virtual-D"}).json()
    with pytest.raises(Exception) as exc_info:
        await objects_router_module.link_object(
            target_dev["id"], ObjectLinkCreate(source_object_id=999999), _fake_request(client),
        )
    assert getattr(exc_info.value, "status_code", None) == 404


async def test_unlink_clears_source_object_id(client, database):
    source_dev, source_obj = _make_device_and_object(client, instance=4250, name="Source-H")
    target_dev = client.post("/devices", json={"protocol": "bacnet", "device_instance": 4251, "name": "Virtual-H"}).json()
    linked = database.create_linked_object(target_dev["id"], source_obj["id"])

    result = await objects_router_module.unlink_object(target_dev["id"], linked["id"], _fake_request(client))
    assert result["source_object_id"] is None
    assert database.get_link_consumers(source_obj["id"]) == []


async def test_unlink_rejects_non_linked_object(client):
    _dev, obj = _make_device_and_object(client, instance=4252, name="Plain-Dev")
    with pytest.raises(Exception) as exc_info:
        await objects_router_module.unlink_object(_dev["id"], obj["id"], _fake_request(client))
    assert getattr(exc_info.value, "status_code", None) == 400


async def test_delete_source_object_blocked_while_linked(client, database):
    source_dev, source_obj = _make_device_and_object(client, instance=4209, name="Source-E")
    target_dev = client.post("/devices", json={"protocol": "bacnet", "device_instance": 4210, "name": "Virtual-E"}).json()
    database.create_linked_object(target_dev["id"], source_obj["id"])

    with pytest.raises(Exception) as exc_info:
        await objects_router_module.delete_object(source_dev["id"], source_obj["id"], _fake_request(client))
    assert getattr(exc_info.value, "status_code", None) == 409

    assert database.get_object(source_obj["id"]) is not None


# ─── API guards on devices_router (real HTTP -- this router imports fine) ──────

def test_delete_source_device_blocked_while_its_object_is_linked(client, database):
    source_dev, source_obj = _make_device_and_object(client, instance=4211, name="Source-F")
    target_dev = client.post("/devices", json={"protocol": "bacnet", "device_instance": 4212, "name": "Virtual-F"}).json()
    database.create_linked_object(target_dev["id"], source_obj["id"])

    resp = client.delete(f"/devices/{source_dev['id']}")
    assert resp.status_code == 409

    still_there = client.get(f"/devices/{source_dev['id']}")
    assert still_there.status_code == 200


def test_switching_device_to_mirror_mode_blocked_while_holding_a_link(client, database):
    source_dev, source_obj = _make_device_and_object(client, instance=4213, name="Source-G")
    other_dev = client.post("/devices", json={"protocol": "bacnet", "device_instance": 4214, "name": "Other-G"}).json()
    target_dev = client.post("/devices", json={"protocol": "bacnet", "device_instance": 4215, "name": "Virtual-G"}).json()
    database.create_linked_object(target_dev["id"], source_obj["id"])

    resp = client.put(f"/devices/{target_dev['id']}", json={
        "protocol": "bacnet", "device_instance": 4215, "name": "Virtual-G",
        "simulation_mode": "mirror", "source_device_id": other_dev["id"],
    })
    assert resp.status_code == 409

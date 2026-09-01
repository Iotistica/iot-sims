"""Coverage for the Simulation Model mapper's one-hop Brick controller
topology scoping:
  - Database.get_controller_topology_point_ids (src/db/database.py)
  - GET /simulation/points/options?device_id= (src/api/routers/simulation.py)
  - discover_candidates(..., allowed_point_ids=...) (src/simulation/mapping/suggestions.py)

Builds the exact RTU / VAV-1 / Zone-1 example from the spec:
    RTU Controller -> controls -> RTU
    RTU -> feeds -> VAV-1
    VAV-1 Controller -> controls -> VAV-1
    VAV-1 -> feeds -> Zone 1
plus an unrelated VAV-2/Boiler pair that must never appear in RTU's scope.

Points are created via Database.create_object directly rather than through
POST /devices/{id}/objects -- that router isn't registered on this Windows
dev machine (it transitively imports discovery.py's `fcntl`, a pre-existing,
unrelated environment limitation; see tests/conftest.py's _routers()), but
create_object has no such dependency and is exactly what the route calls.
"""
from __future__ import annotations

from src.simulation.mapping.suggestions import discover_candidates
from src.simulation.models.registry import VariableDefinition


def _controller_device(client, instance: int, name: str) -> dict:
    device = client.post("/devices", json={"device_instance": instance, "name": name}).json()
    resp = client.post(f"/devices/{device['id']}/controller")
    assert resp.status_code == 200, resp.text
    return device


def _point(database, device_id: int, instance: int, name: str) -> dict:
    return database.create_object(device_id, {
        "object_type": "analog-input", "object_instance": instance, "name": name,
        "units": "degrees-celsius", "behavior": "constant", "behavior_params": '{"value":0}',
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected",
        "polarity": "normal", "point_type": None,
    })


def _equipment(client, name: str, equipment_type: str) -> dict:
    resp = client.post("/equipment", json={"name": name, "equipment_type": equipment_type})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _equipment_entity_id(client, equipment_id: int) -> int:
    matches = client.get("/semantic-entities", params={"entity_kind": "equipment", "equipment_id": equipment_id}).json()
    assert len(matches) == 1
    return matches[0]["id"]


def _controller_entity_id(client, device_id: int) -> int:
    matches = client.get("/semantic-entities", params={"entity_kind": "controller", "device_id": device_id}).json()
    assert len(matches) == 1
    return matches[0]["id"]


def _controls(client, controller_entity_id: int, equipment_entity_id: int) -> None:
    resp = client.post("/semantic-relationships", json={
        "source_entity_id": controller_entity_id, "predicate": "controls", "target_entity_id": equipment_entity_id,
    })
    assert resp.status_code == 201, resp.text


def _feeds(client, source_entity_id: int, target_entity_id: int) -> None:
    resp = client.post("/semantic-relationships", json={
        "source_entity_id": source_entity_id, "predicate": "feeds", "target_entity_id": target_entity_id,
    })
    assert resp.status_code == 201, resp.text


def _assign_point(client, object_id: int, brick_class: str, target_entity_id: int) -> None:
    """Mirrors EquipmentPanel.vue's doAssignPoints: create the point's own
    semantic entity (if missing) then an isPointOf edge to target_entity_id
    (an Equipment or Location entity)."""
    entity = client.post("/semantic-entities", json={
        "name": f"point-{object_id}", "brick_class": brick_class, "entity_kind": "point", "object_id": object_id,
    }).json()
    resp = client.post("/semantic-relationships", json={
        "source_entity_id": entity["id"], "predicate": "isPointOf", "target_entity_id": target_entity_id,
    })
    assert resp.status_code == 201, resp.text


def _build_rtu_vav_topology(client, database):
    """RTU Controller -controls-> RTU -feeds-> VAV-1 <-controls- VAV-1
    Controller; plus an unrelated VAV-2 device/controller and a Boiler
    device that share none of this."""
    rtu_controller = _controller_device(client, 3001, "RTU Controller")
    rtu_sat = _point(database, rtu_controller["id"], 1, "RTU-SAT")

    vav1_controller = _controller_device(client, 3002, "VAV-1 Controller")
    vav1_zone_temp = _point(database, vav1_controller["id"], 1, "VAV1-ZoneTemp")

    rtu_equipment = _equipment(client, "RTU", "Air_Handling_Unit")
    vav1_equipment = _equipment(client, "VAV-1", "Variable_Air_Volume_Box")

    _controls(client, _controller_entity_id(client, rtu_controller["id"]), _equipment_entity_id(client, rtu_equipment["id"]))
    _controls(client, _controller_entity_id(client, vav1_controller["id"]), _equipment_entity_id(client, vav1_equipment["id"]))
    _feeds(client, _equipment_entity_id(client, rtu_equipment["id"]), _equipment_entity_id(client, vav1_equipment["id"]))

    # Unrelated equipment/controller -- must never appear in RTU's scope.
    vav2_controller = _controller_device(client, 3003, "VAV-2 Controller")
    vav2_point = _point(database, vav2_controller["id"], 1, "VAV2-ZoneTemp")
    vav2_equipment = _equipment(client, "VAV-2", "Variable_Air_Volume_Box")
    _controls(client, _controller_entity_id(client, vav2_controller["id"]), _equipment_entity_id(client, vav2_equipment["id"]))

    boiler_controller = _controller_device(client, 3004, "Boiler Controller")
    boiler_point = _point(database, boiler_controller["id"], 1, "Boiler-SupplyTemp")
    boiler_equipment = _equipment(client, "Boiler-1", "Boiler")
    _controls(client, _controller_entity_id(client, boiler_controller["id"]), _equipment_entity_id(client, boiler_equipment["id"]))

    return {
        "rtu_controller": rtu_controller, "rtu_sat": rtu_sat,
        "vav1_controller": vav1_controller, "vav1_zone_temp": vav1_zone_temp,
        "vav2_point": vav2_point, "boiler_point": boiler_point,
    }


def test_scope_includes_anchor_and_feeds_equipment_excludes_unrelated(client, database):
    ctx = _build_rtu_vav_topology(client, database)

    point_ids = database.get_controller_topology_point_ids(ctx["rtu_controller"]["id"])
    assert point_ids == {ctx["rtu_sat"]["id"], ctx["vav1_zone_temp"]["id"]}
    assert ctx["vav2_point"]["id"] not in point_ids
    assert ctx["boiler_point"]["id"] not in point_ids


def test_point_options_endpoint_scoped_by_device_id(client, database):
    ctx = _build_rtu_vav_topology(client, database)

    resp = client.get("/simulation/points/options", params={"device_id": ctx["rtu_controller"]["id"]})
    assert resp.status_code == 200
    ids = {p["id"] for p in resp.json()}
    assert ids == {ctx["rtu_sat"]["id"], ctx["vav1_zone_temp"]["id"]}


def test_point_options_endpoint_falls_back_when_no_controller_entity(client, database):
    """A device with no Controller role at all -- get_controller_topology_point_ids
    returns None, and the endpoint must fall back to the full unscoped list,
    exactly like calling it with no device_id at all."""
    plain_device = client.post("/devices", json={"device_instance": 3005, "name": "Legacy Device"}).json()
    _build_rtu_vav_topology(client, database)

    unscoped = client.get("/simulation/points/options").json()
    scoped_but_unresolvable = client.get(
        "/simulation/points/options", params={"device_id": plain_device["id"]},
    ).json()
    assert {p["id"] for p in scoped_but_unresolvable} == {p["id"] for p in unscoped}
    assert database.get_controller_topology_point_ids(plain_device["id"]) is None


def test_location_fed_by_anchor_contributes_its_own_points_only(client, database):
    """RTU -feeds-> Zone 1 (Location): points isPointOf Zone 1 directly are
    included, but equipment merely *sited* at Zone 1 via hasLocation (not
    fed by RTU) must NOT be pulled in -- a Location is a scope leaf, never
    expanded back out to discover more equipment."""
    ctx = _build_rtu_vav_topology(client, database)

    zone = client.post("/locations", json={"name": "Zone 1", "kind": "Room"}).json()
    zone_entity = client.get(
        "/semantic-entities", params={"entity_kind": "location", "location_id": zone["id"]},
    ).json()[0]
    zone_occ_object = _point(database, ctx["rtu_controller"]["id"], 2, "Zone1-Occ")
    zone_point = client.post("/semantic-entities", json={
        "name": "Zone1-Occupancy", "brick_class": "Occupancy_Sensor", "entity_kind": "point",
        "object_id": zone_occ_object["id"],
    }).json()
    client.post("/semantic-relationships", json={
        "source_entity_id": zone_point["id"], "predicate": "isPointOf", "target_entity_id": zone_entity["id"],
    })

    # RTU -feeds-> Zone 1
    rtu_equipment = next(e for e in client.get("/equipment").json() if e["name"] == "RTU")
    _feeds(client, _equipment_entity_id(client, rtu_equipment["id"]), zone_entity["id"])

    # An unrelated piece of equipment merely located AT Zone 1 (hasLocation),
    # never fed by RTU -- must stay excluded.
    sited_elsewhere = _equipment(client, "Unrelated-In-Zone1", "Pump")
    client.post("/semantic-relationships", json={
        "source_entity_id": _equipment_entity_id(client, sited_elsewhere["id"]), "predicate": "hasLocation", "target_entity_id": zone_entity["id"],
    })
    sited_controller = _controller_device(client, 3006, "Unrelated Pump Controller")
    sited_point = _point(database, sited_controller["id"], 1, "Pump-Status")
    _controls(client, _controller_entity_id(client, sited_controller["id"]), _equipment_entity_id(client, sited_elsewhere["id"]))

    point_ids = database.get_controller_topology_point_ids(ctx["rtu_controller"]["id"])
    assert ctx["rtu_sat"]["id"] in point_ids
    assert ctx["vav1_zone_temp"]["id"] in point_ids
    assert zone_occ_object["id"] in point_ids
    assert sited_point["id"] not in point_ids


def test_no_controller_role_returns_none_not_empty_set(client, database):
    device = client.post("/devices", json={"device_instance": 3007, "name": "No Role"}).json()
    assert database.get_controller_topology_point_ids(device["id"]) is None


def test_controller_controlling_no_equipment_returns_none(client, database):
    controller = _controller_device(client, 3008, "Idle Controller")
    assert database.get_controller_topology_point_ids(controller["id"]) is None


def test_discover_candidates_allowed_point_ids_filters_result(client, database):
    ahu = client.post("/devices", json={"device_instance": 3009, "name": "AHU-Filter"}).json()
    keep = _point(database, ahu["id"], 1, "Keep")
    drop = _point(database, ahu["id"], 2, "Drop")

    variable = VariableDefinition("x", "X", "input")

    unfiltered, _, _ = discover_candidates(variable, ahu["id"], database)
    ids_unfiltered = {c.id for c in unfiltered}
    assert {keep["id"], drop["id"]} <= ids_unfiltered

    filtered, _, _ = discover_candidates(
        variable, ahu["id"], database, allowed_point_ids={keep["id"]},
    )
    assert {c.id for c in filtered} == {keep["id"]}


def test_discover_candidates_allowed_point_ids_none_is_passthrough(client, database):
    ahu = client.post("/devices", json={"device_instance": 3010, "name": "AHU-Passthrough"}).json()
    _point(database, ahu["id"], 1, "Pt")
    variable = VariableDefinition("x", "X", "input")

    with_none, scope_a, _ = discover_candidates(variable, ahu["id"], database, allowed_point_ids=None)
    without_arg, scope_b, _ = discover_candidates(variable, ahu["id"], database)
    assert {c.id for c in with_none} == {c.id for c in without_arg}
    assert scope_a == scope_b


def test_equipments_own_points_preferred_over_shared_device_object_scan(client, database):
    """Reproduces the real leak: a VAV controller's BACnet device can
    physically host a point that's semantically isPointOf a DIFFERENT
    entity two hops away (e.g. a zone temperature sensor wired directly
    into the VAV controller, but isPointOf the Zone Location, not the VAV
    Equipment). Once VAV-1 has its OWN direct point assignment, that
    assignment -- not "every object on VAV-1's controller device" -- must
    be what RTU's topology scope uses, so the co-hosted Zone point stays
    excluded."""
    rtu_controller = _controller_device(client, 3013, "RTU Controller 3")
    rtu_equipment = _equipment(client, "RTU-3", "Air_Handling_Unit")
    _controls(client, _controller_entity_id(client, rtu_controller["id"]), _equipment_entity_id(client, rtu_equipment["id"]))

    vav1_controller = _controller_device(client, 3014, "VAV-1 Controller 3")
    vav1_equipment = _equipment(client, "VAV-1-3", "Variable_Air_Volume_Box")
    vav1_entity_id = _equipment_entity_id(client, vav1_equipment["id"])
    _controls(client, _controller_entity_id(client, vav1_controller["id"]), vav1_entity_id)
    _feeds(client, _equipment_entity_id(client, rtu_equipment["id"]), vav1_entity_id)

    zone1 = client.post("/locations", json={"name": "Zone 1-3", "kind": "Zone"}).json()
    zone1_entity = client.get(
        "/semantic-entities", params={"entity_kind": "location", "location_id": zone1["id"]},
    ).json()[0]
    _feeds(client, vav1_entity_id, zone1_entity["id"])

    # VAV-1's own damper point AND a zone sensor are both physically wired
    # into the same VAV-1 Controller device.
    damper_point = _point(database, vav1_controller["id"], 1, "VAV1-Damper")
    zone_temp_point = _point(database, vav1_controller["id"], 2, "Zone1-Temp-on-VAV1-device")

    # Only the damper point is semantically assigned to VAV-1 Equipment;
    # the zone sensor is assigned to Zone-1 instead (its real owner).
    _assign_point(client, damper_point["id"], "Damper_Position_Status", vav1_entity_id)
    _assign_point(client, zone_temp_point["id"], "Zone_Air_Temperature_Sensor", zone1_entity["id"])

    point_ids = database.get_controller_topology_point_ids(rtu_controller["id"])
    assert damper_point["id"] in point_ids
    assert zone_temp_point["id"] not in point_ids


def test_equipment_with_no_direct_points_still_falls_back_to_controller_device(client, database):
    """Baseline regression: an Equipment with zero direct point
    assignments keeps today's fallback (every object on its controlling
    device) -- this plan explicitly keeps that fallback for the
    not-yet-classified case, it only stops trusting it once a real
    assignment exists."""
    ctx = _build_rtu_vav_topology(client, database)
    point_ids = database.get_controller_topology_point_ids(ctx["rtu_controller"]["id"])
    assert ctx["vav1_zone_temp"]["id"] in point_ids

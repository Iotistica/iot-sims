"""Project import/export round-trip safety for Brick Core semantic data.

load_project() always reassigns fresh autoincrement ids on every call
(DELETE then re-INSERT, even reloading the exact same stored snapshot) --
semantic_key embeds those surrogate ids, so it must be RECOMPUTED from the
newly-assigned ones, never copied verbatim from the stored JSON. These
tests prove the three concrete failure modes that would result if that
recomputation (or the pre-emptive DELETE FROM semantic_entities/
semantic_relationships at the top of load_project()) were missing or wrong."""
from __future__ import annotations

from src.semantics.resolver import SemanticResolver
from src.simulation.model_store import create_simulation_model, list_simulation_models


def _object_id(seeded_database, device_id: int, name: str) -> int:
    return next(o["id"] for o in seeded_database.get_objects(device_id) if o["name"] == name)


def _ahu1_entities(database):
    devices = database.get_devices()
    ahu1_id = next(d["id"] for d in devices if d["device_instance"] == 1003)
    ahu_entity = database.get_semantic_entities(
        device_id=ahu1_id, entity_kind="equipment", brick_class="Air_Handling_Unit",
    )[0]
    fans = database.get_related_entities(ahu_entity["id"], "isPartOf", direction="in")
    return ahu1_id, ahu_entity, fans


def test_relationships_survive_id_remapping(seeded_database):
    original_ahu1_id, original_ahu_entity, original_fans = _ahu1_entities(seeded_database)

    project = seeded_database.save_project("Round-trip Test", "")
    seeded_database.load_project(project["id"])

    new_ahu1_id, new_ahu_entity, new_fans = _ahu1_entities(seeded_database)

    # ids were actually reassigned -- otherwise this test would prove nothing.
    assert new_ahu1_id != original_ahu1_id
    assert new_ahu_entity["id"] != original_ahu_entity["id"]
    assert {f["id"] for f in new_fans}.isdisjoint({f["id"] for f in original_fans})

    # The relationship still connects the CORRECT (new) entities, not the
    # stale original ones (which no longer exist in the DB at all).
    assert {f["brick_class"] for f in new_fans} == {"Supply_Fan", "Return_Fan"}
    for fan in new_fans:
        related_back = seeded_database.get_related_entities(fan["id"], "isPartOf", direction="out")
        assert related_back and related_back[0]["id"] == new_ahu_entity["id"]

    # semantic_key reflects the NEW device_id -- not a copy of the stored,
    # now-stale value.
    for fan in new_fans:
        assert f"device={new_ahu1_id}" in fan["semantic_key"]
        assert f"device={original_ahu1_id}" not in fan["semantic_key"]


def test_semantic_lookup_works_after_import(seeded_database):
    devices = seeded_database.get_devices()
    gw1_id = next(d["id"] for d in devices if d["device_instance"] == 1501)

    project = seeded_database.save_project("Round-trip Test", "")
    seeded_database.load_project(project["id"])

    devices = seeded_database.get_devices()
    new_gw1_id = next(d["id"] for d in devices if d["device_instance"] == 1501)
    assert new_gw1_id != gw1_id

    resolver = SemanticResolver(database=seeded_database, simulation_engine=None)
    zone_a = resolver.get_lighting_zone_entity(new_gw1_id, "zone-a")
    assert zone_a is not None
    assert zone_a["local_slug"] == "zone-a"

    # Looking it up by the STALE device id must fail -- proves the lookup
    # is genuinely keyed off the current device_id, not some leftover cache.
    assert resolver.get_lighting_zone_entity(gw1_id, "zone-a") is None


def test_energy_model_configs_survive_project_reload(seeded_database):
    """energy_model_configs was never part of save_project()/load_project()'s
    exported JSON, and device_id ON DELETE CASCADEs -- so every project
    reload silently deleted the seeded Chiller-Plant's energy model and
    never recreated it, leaving Utilities/energy history permanently empty
    for that project. See save_project()/load_project()."""
    devices = seeded_database.get_devices()
    old_chiller_id = next(d["id"] for d in devices if d["device_instance"] == 1001)

    configs_before = seeded_database.get_enabled_energy_model_configs()
    assert any(
        c["device_id"] == old_chiller_id and c["model_type"] == "chiller"
        for c in configs_before
    )

    project = seeded_database.save_project("Round-trip Test", "")
    seeded_database.load_project(project["id"])

    devices = seeded_database.get_devices()
    new_chiller_id = next(d["id"] for d in devices if d["device_instance"] == 1001)
    assert new_chiller_id != old_chiller_id  # ids were actually reassigned

    chiller_configs = [
        c for c in seeded_database.get_enabled_energy_model_configs()
        if c["model_type"] == "chiller"
    ]
    assert len(chiller_configs) == 1
    assert chiller_configs[0]["device_id"] == new_chiller_id


def test_energy_model_configs_no_duplicates_on_repeated_reload(seeded_database):
    project = seeded_database.save_project("Round-trip Test", "")

    seeded_database.load_project(project["id"])
    seeded_database.load_project(project["id"])
    seeded_database.load_project(project["id"])

    chiller_configs = [
        c for c in seeded_database.get_enabled_energy_model_configs()
        if c["model_type"] == "chiller"
    ]
    assert len(chiller_configs) == 1


def test_energy_model_configs_preserve_instance_key_on_reload(seeded_database):
    """Multi-instance-key configs (e.g. lighting zone-a/zone-b on one DALI
    gateway) must survive a project reload with their instance_key intact,
    device_id remapped correctly, and without colliding with each other."""
    devices = seeded_database.get_devices()
    old_gateway_id = next(d["id"] for d in devices if d["device_instance"] == 1501)

    seeded_database.upsert_energy_model_config(
        old_gateway_id, "lighting", '{"rated_power_kw": 3.0}', True, instance_key="zone-a",
    )
    seeded_database.upsert_energy_model_config(
        old_gateway_id, "lighting", '{"rated_power_kw": 2.5}', True, instance_key="zone-b",
    )

    project = seeded_database.save_project("Round-trip Test", "")
    seeded_database.load_project(project["id"])

    devices = seeded_database.get_devices()
    new_gateway_id = next(d["id"] for d in devices if d["device_instance"] == 1501)
    assert new_gateway_id != old_gateway_id

    lighting_configs = seeded_database.get_energy_model_configs(new_gateway_id)
    lighting_configs = [c for c in lighting_configs if c["model_type"] == "lighting"]
    assert {c["instance_key"] for c in lighting_configs} == {"zone-a", "zone-b"}
    assert all(c["device_id"] == new_gateway_id for c in lighting_configs)


def test_external_device_source_type_survives_project_reload(seeded_database):
    """load_project()'s INSERT INTO devices/objects use explicit, hand-
    written column lists (not a generic SELECT *) -- source_type and the
    external_* columns must be included there explicitly or a saved
    external-BACnet device would silently come back as 'simulated' on
    every reload, which is exactly the safety-boundary regression the
    external-device feature depends on never happening."""
    seeded_database.sync_external_devices([{
        "device_instance": 9001,
        "name": "ext_ahu_9001",
        "host": "172.22.0.99",
        "port": 47808,
        "metadata": {
            "objectName": "External-AHU",
            "vendorName": "Acme Controls",
            "modelName": "X100",
            "vendorId": 42,
            "description": "a real physical device",
        },
    }])
    ext_device = next(d for d in seeded_database.get_devices() if d["device_instance"] == 9001)
    seeded_database.sync_external_objects(ext_device["id"], [
        {"object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius", "description": "supply air temp"},
    ])

    project = seeded_database.save_project("Round-trip Test", "")
    seeded_database.load_project(project["id"])

    reloaded = next(d for d in seeded_database.get_devices() if d["device_instance"] == 9001)
    assert reloaded["source_type"] == "external-bacnet"
    assert reloaded["external_host"] == "172.22.0.99"
    assert reloaded["external_port"] == 47808
    assert reloaded["external_vendor_id"] == 42
    assert reloaded["vendor_name"] == "Acme Controls"
    assert reloaded["description"] == "a real physical device"

    reloaded_objects = seeded_database.get_objects(reloaded["id"])
    assert len(reloaded_objects) == 1
    assert reloaded_objects[0]["description"] == "supply air temp"

    # Every other seeded (simulated) device must be unaffected.
    simulated = [d for d in seeded_database.get_devices() if d["device_instance"] != 9001]
    assert all(d["source_type"] == "simulated" for d in simulated)


def test_no_semantic_key_collisions_on_repeated_reload(seeded_database):
    project = seeded_database.save_project("Round-trip Test", "")

    # Reload the SAME project multiple times in a row -- each call wipes
    # and reassigns fresh ids; must never hit a semantic_key UNIQUE
    # constraint violation from stale rows surviving the wipe.
    seeded_database.load_project(project["id"])
    seeded_database.load_project(project["id"])
    seeded_database.load_project(project["id"])

    entities = seeded_database.get_semantic_entities()
    assert len(entities) > 0
    semantic_keys = [e["semantic_key"] for e in entities if e["semantic_key"] is not None]
    assert len(semantic_keys) == len(set(semantic_keys))


def test_simulation_models_survive_project_reload(seeded_database):
    """simulation_model_configs (and its mappings) was never part of
    save_project()/update_project()'s exported JSON -- see
    list_all_simulation_models()/insert_simulation_model() in
    model_store.py and their use in legacy.py's save_project()/
    update_project()/load_project(). Every project reload silently
    deleted every simulation model and never recreated it, the same
    class of bug as test_energy_model_configs_survive_project_reload
    above, but for simulation models instead of energy models."""
    devices = seeded_database.get_devices()
    ahu1_id = next(d["id"] for d in devices if d["device_instance"] == 1003)
    sat_point_id = _object_id(seeded_database, ahu1_id, "SAT")

    created = create_simulation_model(
        seeded_database,
        name="AHU-1 Model",
        provider_type="fmu",
        model_type="SimpleAHU",
        enabled=False,
        parameters={"foo": "bar"},
        created_from_device_id=ahu1_id,
        mappings=[{"variable": "supply_air_temp_c", "direction": "output", "point_id": sat_point_id}],
    )

    project = seeded_database.save_project("Round-trip Test", "")
    seeded_database.load_project(project["id"])

    devices = seeded_database.get_devices()
    new_ahu1_id = next(d["id"] for d in devices if d["device_instance"] == 1003)
    assert new_ahu1_id != ahu1_id  # ids were actually reassigned

    matches = [m for m in list_simulation_models(seeded_database) if m["name"] == created["name"]]
    assert len(matches) == 1
    reloaded = matches[0]

    assert reloaded["id"] != created["id"]  # a genuinely new row, not the stale one
    assert reloaded["created_from_device_id"] == new_ahu1_id
    assert reloaded["parameters"] == {"foo": "bar"}
    assert len(reloaded["mappings"]) == 1
    mapping = reloaded["mappings"][0]
    assert mapping["variable"] == "supply_air_temp_c"
    assert mapping["direction"] == "output"
    new_sat_point_id = _object_id(seeded_database, new_ahu1_id, "SAT")
    assert mapping["point_id"] == new_sat_point_id
    assert mapping["point_id"] != sat_point_id


def test_simulation_model_weighted_average_mapping_survives_project_reload(seeded_database):
    """Aggregate (weighted_average) mappings reference points on OTHER
    devices (e.g. an RTU's return-air temp = weighted average of two VAV
    zone temps, weighted by each zone's airflow) -- exactly the
    cross-device-reference shape that made the plain-mapping bug above
    dangerous to get wrong: point_ids/weight_point_ids must be remapped via
    the SAME global_obj_id_map used for every other point-referencing
    table, and must stay positionally aligned (value[i] <-> weight[i])
    after the remap."""
    devices = seeded_database.get_devices()
    vav1_id = next(d["id"] for d in devices if d["device_instance"] == 1101)
    vav2_id = next(d["id"] for d in devices if d["device_instance"] == 1102)

    vav1_temp = _object_id(seeded_database, vav1_id, "Zone-Temp")
    vav1_flow = _object_id(seeded_database, vav1_id, "Zone-Airflow")
    vav2_temp = _object_id(seeded_database, vav2_id, "Zone-Temp")
    vav2_flow = _object_id(seeded_database, vav2_id, "Zone-Airflow")

    created = create_simulation_model(
        seeded_database,
        name="Floor-1 Weighted RAT",
        provider_type="fmu",
        model_type="SimpleAHU",
        enabled=False,
        parameters={},
        created_from_device_id=None,
        mappings=[],
        aggregate_mappings=[{
            "variable": "return_air_temp_c",
            "direction": "input",
            "operation": "weighted_average",
            "point_ids": [vav1_temp, vav2_temp],
            "weight_point_ids": [vav1_flow, vav2_flow],
        }],
    )

    project = seeded_database.save_project("Round-trip Test", "")
    seeded_database.load_project(project["id"])

    matches = [m for m in list_simulation_models(seeded_database) if m["name"] == "Floor-1 Weighted RAT"]
    assert len(matches) == 1
    reloaded = matches[0]
    assert reloaded["id"] != created["id"]

    devices = seeded_database.get_devices()
    new_vav1_id = next(d["id"] for d in devices if d["device_instance"] == 1101)
    new_vav2_id = next(d["id"] for d in devices if d["device_instance"] == 1102)
    new_vav1_temp = _object_id(seeded_database, new_vav1_id, "Zone-Temp")
    new_vav1_flow = _object_id(seeded_database, new_vav1_id, "Zone-Airflow")
    new_vav2_temp = _object_id(seeded_database, new_vav2_id, "Zone-Temp")
    new_vav2_flow = _object_id(seeded_database, new_vav2_id, "Zone-Airflow")

    assert len(reloaded["mappings"]) == 1
    mapping = reloaded["mappings"][0]
    assert mapping["operation"] == "weighted_average"
    # Positional alignment survives the remap: value[i] still pairs with
    # weight[i], not some other row's weight.
    assert mapping["point_ids"] == [new_vav1_temp, new_vav2_temp]
    assert mapping["weight_point_ids"] == [new_vav1_flow, new_vav2_flow]
    # And the ids genuinely changed -- the stale ones must be gone.
    assert set(mapping["point_ids"]).isdisjoint({vav1_temp, vav2_temp})
    assert set(mapping["weight_point_ids"]).isdisjoint({vav1_flow, vav2_flow})


def test_simulation_models_no_duplicates_on_repeated_project_reload(seeded_database):
    devices = seeded_database.get_devices()
    ahu1_id = next(d["id"] for d in devices if d["device_instance"] == 1003)
    sat_point_id = _object_id(seeded_database, ahu1_id, "SAT")

    create_simulation_model(
        seeded_database,
        name="AHU-1 Model",
        provider_type="fmu",
        model_type="SimpleAHU",
        enabled=False,
        parameters={},
        created_from_device_id=ahu1_id,
        mappings=[{"variable": "supply_air_temp_c", "direction": "output", "point_id": sat_point_id}],
    )

    project = seeded_database.save_project("Round-trip Test", "")

    seeded_database.load_project(project["id"])
    seeded_database.load_project(project["id"])
    seeded_database.load_project(project["id"])

    matches = [m for m in list_simulation_models(seeded_database) if m["name"] == "AHU-1 Model"]
    assert len(matches) == 1

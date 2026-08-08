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

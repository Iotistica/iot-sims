"""Tests for Database.generate_building_levels() and the sort ordering it
introduces into get_locations().

Coverage targets from the original plan:
- Building + Level hierarchy created correctly (correct count, names, kinds)
- Basement sort_order is negative; above-ground is positive
- get_locations() returns levels in physical order: B2, B1, L1, L2, ...
- 0/0 is a no-op (no locations created)
- sort_order survives a save_project() / load_project() round-trip
"""
from __future__ import annotations

import pytest

from src.legacy import Database


# ── helpers ──────────────────────────────────────────────────────────────────

def _names(locations: list[dict]) -> list[str]:
    return [l["name"] for l in locations]


def _by_name(locations: list[dict], name: str) -> dict:
    return next(l for l in locations if l["name"] == name)


# ── generate_building_levels ──────────────────────────────────────────────────

def test_noop_when_both_zero(database: Database):
    database.generate_building_levels("Empty Project", 0, 0)
    assert database.get_locations() == []


def test_above_ground_only(database: Database):
    database.generate_building_levels("Tower", 3, 0)
    locs = database.get_locations()
    assert len(locs) == 4  # Building + L1, L2, L3

    building = _by_name(locs, "Tower")
    assert building["kind"] == "Building"
    assert building["sort_order"] is None  # Building root has no sort_order

    for i, name in enumerate(["L1", "L2", "L3"], start=1):
        loc = _by_name(locs, name)
        assert loc["kind"] == "Floor"
        assert loc["sort_order"] == i
        assert loc["parent_location_id"] == building["id"]


def test_below_ground_only(database: Database):
    database.generate_building_levels("Bunker", 0, 2)
    locs = database.get_locations()
    assert len(locs) == 3  # Building + B1, B2

    building = _by_name(locs, "Bunker")
    b1 = _by_name(locs, "B1")
    b2 = _by_name(locs, "B2")

    assert b1["sort_order"] == -1
    assert b2["sort_order"] == -2
    assert b1["parent_location_id"] == building["id"]
    assert b2["parent_location_id"] == building["id"]


def test_mixed_levels_correct_count(database: Database):
    database.generate_building_levels("HQ", 4, 2)
    locs = database.get_locations()
    # Building + B2 + B1 + L1 + L2 + L3 + L4
    assert len(locs) == 7


def test_get_locations_physical_order(database: Database):
    """get_locations() must return B2, B1, L1, L2, L3, L4 — NOT alphabetical."""
    database.generate_building_levels("HQ", 4, 2)
    # Filter to just the level names (exclude the Building root which has sort_order=NULL)
    levels = [l for l in database.get_locations() if l["kind"] == "Floor"]
    assert _names(levels) == ["B2", "B1", "L1", "L2", "L3", "L4"]


def test_manual_location_sorts_after_levels(database: Database):
    """A manually-created Location (sort_order=NULL) sorts after all auto-levels."""
    database.generate_building_levels("HQ", 2, 1)
    # Add a child of the Building with no sort_order (manual)
    locs = database.get_locations()
    building_id = next(l["id"] for l in locs if l["kind"] == "Building")
    database.create_location("Zone A", building_id, "", None)

    children = [l for l in database.get_locations() if l["parent_location_id"] == building_id]
    child_names = _names(children)
    # Levels first, then manual alphabetically
    assert child_names == ["B1", "L1", "L2", "Zone A"]


# ── sort_order survives project round-trip ────────────────────────────────────

def test_sort_order_survives_save_and_load(database: Database):
    database.generate_building_levels("Office", 3, 1)
    original_order = _names(database.get_locations())

    # snapshot + restore
    saved = database.save_project("Office", "desc")
    database.load_project(saved["id"])

    restored_order = _names(database.get_locations())
    assert restored_order == original_order


def test_load_project_restores_sort_order_values(database: Database):
    database.generate_building_levels("Park", 2, 2)
    saved = database.save_project("Park", "")
    database.load_project(saved["id"])

    locs = {l["name"]: l for l in database.get_locations()}
    assert locs["B2"]["sort_order"] == -2
    assert locs["B1"]["sort_order"] == -1
    assert locs["L1"]["sort_order"] == 1
    assert locs["L2"]["sort_order"] == 2


def test_empty_generated_level_can_be_deleted_with_semantic_mirror(database: Database):
    database.generate_building_levels("Tower", 1, 2)
    locs = {l["name"]: l for l in database.get_locations()}

    b1 = locs["B1"]
    assert database.get_semantic_entities(location_id=b1["id"])

    assert database.delete_location(b1["id"]) is True
    assert database.get_location(b1["id"]) is None
    assert database.get_semantic_entities(location_id=b1["id"]) == []

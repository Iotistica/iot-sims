"""object_factory.create_object()'s optional State_Text wiring for a multi-state object (see
migration 031 / schemas.py's ObjectCreate.state_text) -- primarily populated by the classifier
attach flow from a promoted model's class_labels, but this exercises the construction-layer
plumbing directly (bypassing the HTTP client entirely, same reasoning/pattern as
test_self_referential_fmu_mapping.py's own create_object()-driving tests: this Windows dev
environment can't import objects_router -- external_objects.py -> discovery.py -> `import fcntl`
-- so an HTTP-level test of object creation can't run here; object_factory.create_object() is
unit-tested directly instead, and is the exact function POST/PUT /devices/{id}/objects ultimately
feeds into)."""
from __future__ import annotations

import json

from src.bacnet import object_factory
from src.simulation.engine import SimEngine


def _multistate_row(
    obj_id: int, *, number_of_states: int, state_text=None, value: float = 0.0, name: str = "Detection-Point",
) -> dict:
    return {
        "id": obj_id,
        "object_type": "multi-state-value",
        "object_instance": 1,
        "name": name,
        "units": "no-units",
        "behavior": "constant",
        "behavior_params": json.dumps({"value": value}),
        "manual_value": None,
        "number_of_states": number_of_states,
        "reliability": "no-fault-detected",
        "state_text": json.dumps(state_text) if state_text is not None else None,
    }


async def test_state_text_applied_to_multistate_value_object(database):
    engine = SimEngine(database)
    class_labels = ["normal", "sensor_bias", "sensor_stuck"]
    obj_row = _multistate_row(20401, number_of_states=3, state_text=class_labels, value=1.0)

    bacnet_obj, _ = object_factory.create_object(engine, obj_row, slot=0, device_name="Test")

    assert list(bacnet_obj.stateText) == class_labels
    # Present-value itself is unaffected by state_text -- still just the
    # ordinary [1, numberOfStates] clamp of the seeded value.
    assert int(bacnet_obj.presentValue) == 1


async def test_no_state_text_leaves_stateText_unset(database):
    """Backward compatibility: every multi-state object before this field
    existed (state_text is None) must behave exactly as before -- no
    stateText property set at all."""
    engine = SimEngine(database)
    obj_row = _multistate_row(20402, number_of_states=2, state_text=None)

    bacnet_obj, _ = object_factory.create_object(engine, obj_row, slot=0, device_name="Test")

    assert not bacnet_obj.stateText


async def test_malformed_state_text_does_not_raise(database):
    """A stale/corrupt state_text value (e.g. hand-edited DB, or a future
    format change) must degrade gracefully -- object creation itself must
    never fail over this optional, cosmetic field."""
    engine = SimEngine(database)
    obj_row = _multistate_row(20403, number_of_states=2, state_text=None)
    obj_row["state_text"] = "not valid json"

    bacnet_obj, _ = object_factory.create_object(engine, obj_row, slot=0, device_name="Test")

    assert not bacnet_obj.stateText
    assert int(bacnet_obj.presentValue) == 1


async def test_state_text_length_shorter_than_number_of_states_still_applies(database):
    """Not cross-validated against number_of_states at this layer (that's
    schemas.py's ObjectCreate.validate_type job, at the write path) --
    object_factory.create_object() just passes through whatever list it's given,
    same "trust the write path already validated it" precedent as every
    other field here."""
    engine = SimEngine(database)
    obj_row = _multistate_row(20404, number_of_states=4, state_text=["a", "b"], value=0.0)

    bacnet_obj, _ = object_factory.create_object(engine, obj_row, slot=0, device_name="Test")

    assert list(bacnet_obj.stateText) == ["a", "b"]

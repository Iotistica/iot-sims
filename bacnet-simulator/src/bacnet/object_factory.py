"""BACnet object construction boundary -- the physical move `SimEngine._create_object()`'s own
comment used to promise. Builds one live bacpypes3 object (and its initial `Behavior`) from a raw
object row dict; called by `BacnetProtocolAdapter` (src/simulation/protocols/bacnet.py), both from
its own `start()` batch and, indirectly via `SimEngine.add_object_hot()`, from a hot-add.

Takes `engine` (the owning `SimEngine`) rather than the handful of individual values it actually
reads (`_used_object_identifiers`, `_point_behaviors`, `_reload_preserved_values`,
`_point_output_owner`, `state`) -- those all live on SimEngine, are read/written together as part
of one object's construction, and threading five separate parameters through instead buys no real
decoupling here (this module already depends on SimEngine's shape, just narrower).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from bacpypes3.basetypes import BinaryPV, EngineeringUnits
from bacpypes3.local.analog import AnalogInputObject, AnalogOutputObject, AnalogValueObject
from bacpypes3.local.binary import BinaryInputObject, BinaryOutputObject, BinaryValueObject
from bacpypes3.local.multistate import MultiStateInputObject, MultiStateOutputObject, MultiStateValueObject
from bacpypes3.primitivedata import Real, Unsigned

from ..core.config import MULTISTATE_TYPES
from ..simulation.behaviors import Behavior, make_behavior
from .app import _apply_polarity, _apply_reliability

log = logging.getLogger("bacnet-sim")


def _allocate_object_identifier(engine: Any, object_type: str, preferred_instance: int) -> int:
    """Return a BACnet object instance not already used for this type."""
    instance = max(0, int(preferred_instance)) % 4_194_303
    while (object_type, instance) in engine._used_object_identifiers:
        instance += 1
        if instance > 4_194_302:
            instance = 0

    engine._used_object_identifiers.add((object_type, instance))
    return instance


def create_object(engine: Any, obj_row: dict, slot: int, device_name: str = "") -> tuple[Any, Behavior]:
    otype = obj_row["object_type"]
    phys = _allocate_object_identifier(
        engine,
        otype,
        slot * 1000 + int(obj_row["object_instance"]),
    )
    behavior = make_behavior(
        obj_row["behavior"],
        obj_row["behavior_params"],
        obj_row.get("manual_value"),
    )
    # Kept for every object, not just provider-owned ones -- provider-owned
    # points read this same instance every tick to apply the configured
    # Behavior on top of the provider's raw value (see
    # _apply_fmu_behavior()); non-provider-owned points ignore it, since
    # BuiltInSimulationProvider keeps its own separate instance.
    obj_id = int(obj_row["id"])
    engine._point_behaviors[obj_id] = behavior
    # A provider-owned "raw"/"constant" point's real value IS whatever
    # the provider last computed, never something derived from
    # behavior_params (which is frequently empty/stale for a point that
    # has only ever displayed a live provider value -- see reload()'s
    # comment). _point_output_owner survives reload() untouched (only
    # _runtime_points/_prev_values/etc. get cleared -- registered providers
    # keep their output claims), so it's already correct here even
    # mid-rebuild. Restoring from the pre-reload snapshot rather than
    # re-seeding from params is what stops a provider-owned point that
    # also feeds its own model as an input (e.g. a fan command echoed
    # back as the FMU's own speed input) from latching at a zeroed
    # seed value forever.
    if (
        obj_id in engine._reload_preserved_values
        and obj_row["behavior"] in ("raw", "constant")
        and engine._point_output_owner.get(obj_id, "builtin") != "builtin"
    ):
        val = engine._reload_preserved_values[obj_id]
    else:
        val = behavior.compute(engine.state)
    # BACnet requires globally unique object names within a single application,
    # even across virtual devices — prefix with device name to guarantee uniqueness.
    obj_name = f"{device_name}.{obj_row['name']}" if device_name else obj_row["name"]

    _ANALOG_CLS = {
        "analog-input":  AnalogInputObject,
        "analog-output": AnalogOutputObject,
        "analog-value":  AnalogValueObject,
    }
    _BINARY_CLS = {
        "binary-input":  BinaryInputObject,
        "binary-output": BinaryOutputObject,
        "binary-value":  BinaryValueObject,
    }
    if otype in _ANALOG_CLS:
        units_str = obj_row.get("units") or "no-units"
        try:
            units = EngineeringUnits(units_str)
        except Exception:
            units = EngineeringUnits("no-units")
        bacnet_obj = _ANALOG_CLS[otype](
            objectIdentifier=f"{otype},{phys}",
            objectName=obj_name,
            presentValue=Real(float(val)),
            units=units,
        )
        _apply_reliability(bacnet_obj, obj_row.get("reliability") or "no-fault-detected")
    elif otype == "binary-output":
        # Pass presentValue= in the constructor so Commandable.__init__ can set
        # relinquishDefault from it (line 87 in bacpypes3/local/cmd.py).
        # _Object.__init__ sets it directly via super().__setattr__, bypassing
        # Commandable.__setattr__, so priorityArray is not accessed before it exists.
        # The tick loop later writes via Commandable.__setattr__ → priorityArray[15]
        # → recalculating() which keeps presentValue up-to-date for ReadProperty.
        active = bool(val) if not isinstance(val, bool) else val
        bacnet_obj = BinaryOutputObject(
            objectIdentifier=f"{otype},{phys}",
            objectName=obj_name,
            presentValue=BinaryPV("active" if active else "inactive"),
        )
        _apply_reliability(bacnet_obj, obj_row.get("reliability") or "no-fault-detected")
        _apply_polarity(bacnet_obj, obj_row.get("polarity") or "normal")
    elif otype in MULTISTATE_TYPES:
        _MULTISTATE_CLS = {
            "multi-state-input":  MultiStateInputObject,
            "multi-state-output": MultiStateOutputObject,
            "multi-state-value":  MultiStateValueObject,
        }
        n_states = max(1, int(obj_row.get("number_of_states") or 2))
        state = max(1, min(n_states, round(float(val))))
        # Same reasoning as binary-output above — multi-state-output is
        # Commandable too, so presentValue must be passed at construction.
        bacnet_obj = _MULTISTATE_CLS[otype](
            objectIdentifier=f"{otype},{phys}",
            objectName=obj_name,
            presentValue=Unsigned(state),
            numberOfStates=Unsigned(n_states),
        )
        _apply_reliability(bacnet_obj, obj_row.get("reliability") or "no-fault-detected")
        # Optional State_Text (see migration 031) -- primarily populated
        # by the classifier attach flow from a promoted model's own
        # class_labels, but works for any multi-state object a caller
        # sets it on. None (every multi-state object before this field
        # existed) leaves stateText unset, same as always.
        raw_state_text = obj_row.get("state_text")
        if raw_state_text:
            try:
                state_text = json.loads(raw_state_text) if isinstance(raw_state_text, str) else raw_state_text
                if isinstance(state_text, list) and state_text:
                    bacnet_obj.stateText = [str(s) for s in state_text]
            except (TypeError, ValueError, json.JSONDecodeError):
                log.warning("Malformed state_text for object id %s; leaving stateText unset", obj_id)
    else:
        active = bool(val) if not isinstance(val, bool) else val
        cls = _BINARY_CLS.get(otype, BinaryInputObject)
        bacnet_obj = cls(
            objectIdentifier=f"{otype},{phys}",
            objectName=obj_name,
            presentValue=BinaryPV("active" if active else "inactive"),
        )
        _apply_reliability(bacnet_obj, obj_row.get("reliability") or "no-fault-detected")
        if otype == "binary-input":
            _apply_polarity(bacnet_obj, obj_row.get("polarity") or "normal")
    # Stashed for BacnetProtocolAdapter.write_point() (src/simulation/protocols/bacnet.py),
    # which is only ever handed (handle, PointValue) -- it has no obj_row of its own to read
    # object_type/the object's persisted reliability from. Same established pattern this
    # codebase already uses for other per-object bookkeeping stashed directly on a bacpypes3
    # object (see sched_bacnet_obj._value_type, self.app._sim_engine in bacnet.py's start()).
    bacnet_obj._sim_object_type = otype
    bacnet_obj._sim_configured_reliability = obj_row.get("reliability") or "no-fault-detected"
    return bacnet_obj, behavior

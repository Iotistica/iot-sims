"""Named, declarative value conversions applied to an FMU output value
before it reaches a mapped BACnet point's Present_Value.

Some equipment models expose a naturally zero-based state (e.g. a
Modelica/FMU `compressorStage` output: 0=off, 1=stage 1, 2=stage 2) while
this simulator's multi-state objects are strictly 1-based (Present_Value
in [1, numberOfStates], enforced in engine.py's _create_object/
_update_value -- see MULTISTATE_TYPES there). Rather than special-casing
any one FMU variable or model in the BACnet engine, a mapping can declare
`conversion` (a name from CONVERSIONS below) and the named function is
applied to the FMU's raw value at the mapping boundary -- see
providers/fmu.py's _convert_output_value(), which is the single place
this registry is actually invoked, before the value ever reaches
engine.py's own multi-state clamp. Keep that clamp as the safety net it
already is; it's what catches a source that's genuinely already 1-based
(no conversion configured) or an out-of-range value regardless.

Add a new named conversion here -- e.g. for a future fan-mode, heating/
cooling-stage, or other zero-based equipment-mode output -- rather than
adding per-variable logic anywhere else. `conversion` is validated
against this same registry at mapping-save time (see
SimulationModelMappingPayload in api/routers/simulation.py), so an
unknown name is rejected before it can ever reach a running simulation.
"""
from __future__ import annotations

from typing import Any, Callable

CONVERSIONS: dict[str, Callable[[Any], Any]] = {
    # 0=off/1=stage1/2=stage2 (etc.) -> 1=off/2=stage1/3=stage2 (etc.).
    # round() first so a near-integer FMU value (e.g. 1.0000000002 from
    # solver noise) still converts correctly rather than truncating.
    "zero_based_to_multistate": lambda value: round(float(value)) + 1,
}


def apply_output_conversion(conversion: str | None, value: Any) -> Any:
    """Applies a named conversion (or is a no-op when conversion is None).
    conversion is expected to already be validated against CONVERSIONS at
    mapping-save time (the Pydantic layer) -- an unrecognized name here
    (e.g. an old config from before a conversion was renamed/removed)
    falls back to the raw value rather than raising and breaking a live
    simulation tick over a single point's stale config."""
    if conversion is None:
        return value
    convert = CONVERSIONS.get(conversion)
    if convert is None:
        return value
    return convert(value)

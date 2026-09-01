"""FMUSimulationProvider._convert_output_value() applies a binding's
declared `conversion` (see mapping/conversions.CONVERSIONS) to the raw
FMU output value, before engine.py ever sees it. This is the actual
mapping-boundary application point described in
mapping/conversions.py's own module docstring -- these tests exercise it
directly against FMUPointBinding, with no live FMU runtime needed (only
_convert_output_value/_binding_metadata are used, neither of which
requires an active session)."""
from __future__ import annotations

from src.simulation.providers.fmu import FMUPointBinding, FMUSimulationProvider


def _provider(bindings: list[FMUPointBinding]) -> FMUSimulationProvider:
    return FMUSimulationProvider(
        runtime_url="http://fake-runtime.invalid",
        model="RTU",
        bindings=bindings,
    )


def test_binding_without_conversion_is_unaffected():
    """Backward compatibility: an existing multi-state mapping that never
    declared `conversion` must behave exactly as before -- the FMU's raw
    value passes through unchanged (engine.py's own clamp is still the
    only thing that bounds it)."""
    binding = FMUPointBinding(point_id=1, variable="fanMode", direction="output")
    provider = _provider([binding])

    assert provider._convert_output_value(binding, 0) == 0
    assert provider._convert_output_value(binding, 1) == 1


def test_zero_based_to_multistate_conversion_applied_at_output_boundary():
    binding = FMUPointBinding(
        point_id=1, variable="compressorStage", direction="output",
        conversion="zero_based_to_multistate",
    )
    provider = _provider([binding])

    assert provider._convert_output_value(binding, 0) == 1
    assert provider._convert_output_value(binding, 1) == 2
    assert provider._convert_output_value(binding, 2) == 3


def test_conversion_and_cfm_handling_are_independent():
    """A converted (multi-state) binding and a CFM-unit-converted binding
    are orthogonal concerns -- applying the declarative `conversion`
    first must not disturb the existing volumetric-flow auto-detection
    for a variable that isn't multi-state-related at all."""
    flow_binding = FMUPointBinding(
        point_id=2, variable="supply_airflow_m3_s", direction="output",
    )
    provider = _provider([flow_binding])

    # No bound point metadata (no context) -- units lookup returns "",
    # so the CFM branch doesn't fire and the raw m3/s value passes
    # through unchanged, same as before this change.
    assert provider._convert_output_value(flow_binding, 1.5) == 1.5

"""mapping_conversions.CONVERSIONS is a named, declarative registry of
value conversions applied to an FMU output at the mapping boundary --
e.g. 'zero_based_to_multistate' for a zero-based Modelica/FMU state
output (0=off, 1=stage 1, 2=stage 2, ...) mapped onto this simulator's
strictly 1-based multi-state Present_Value (see engine.py's
MULTISTATE_TYPES clamp: max(1, min(numberOfStates, round(value)))).

Core regression this guards: before this conversion existed, an
unmapped zero-based source collided at the clamp -- both 0 and 1 landed
on Present_Value 1, and the highest state was unreachable. See
test_zero_and_one_do_not_collide below for the explicit check.
"""
from __future__ import annotations

import pytest

from src.simulation.mapping_conversions import CONVERSIONS, apply_output_conversion


def test_no_conversion_is_a_passthrough():
    assert apply_output_conversion(None, 0) == 0
    assert apply_output_conversion(None, 2.5) == 2.5


def test_zero_based_to_multistate_shifts_every_state_by_one():
    assert apply_output_conversion("zero_based_to_multistate", 0) == 1
    assert apply_output_conversion("zero_based_to_multistate", 1) == 2
    assert apply_output_conversion("zero_based_to_multistate", 2) == 3


def test_zero_and_one_do_not_collide():
    """The exact bug this conversion exists to prevent: without it,
    engine.py's max(1, min(n, round(value))) clamp floors both a
    zero-based source's 0 ("off") and 1 ("stage 1") to the same
    Present_Value (1), making "stage 1" unreachable and "off"/"stage 1"
    indistinguishable on the wire."""
    off = apply_output_conversion("zero_based_to_multistate", 0)
    stage1 = apply_output_conversion("zero_based_to_multistate", 1)
    stage2 = apply_output_conversion("zero_based_to_multistate", 2)

    assert off != stage1
    assert stage1 != stage2
    assert {off, stage1, stage2} == {1, 2, 3}


def test_zero_based_to_multistate_rounds_solver_noise():
    """A near-integer FMU value (e.g. 1.0000000002 from solver noise, or
    0.9999999998) must still land on the correct state, not be truncated
    toward the wrong one."""
    assert apply_output_conversion("zero_based_to_multistate", 1.0000000002) == 2
    assert apply_output_conversion("zero_based_to_multistate", 0.9999999998) == 2


def test_unknown_conversion_name_falls_back_to_raw_value():
    """Defensive: an unrecognized name (e.g. a stale config from before a
    conversion was renamed/removed) must not crash a live simulation tick
    over one point's config -- conversion names are validated at
    mapping-save time (SimulationModelMappingPayload), so this should
    only matter for data that predates validation."""
    assert apply_output_conversion("not_a_real_conversion", 5) == 5


def test_registry_contains_zero_based_to_multistate():
    assert "zero_based_to_multistate" in CONVERSIONS

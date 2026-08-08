"""Present_Value for binary-input/output/value is BACnetBinaryPV (an
ENUMERATED{inactive,active} per ASHRAE 135's object type tables), not a
float or general integer. Behavior.compute() is typed float|bool and
different Behavior subclasses disagree on which they return for the same
logical binary point (ManualBehavior keeps bool only for literal JSON
true/false; DailyPatternBehavior/FaultBehavior always return float) --
this was the root cause of the reported inconsistency (some binary points
displayed "ON"/"OFF", others "0.00"/"1.00", others raw "0"/"1"). See
normalize_present_value() in src/legacy.py, called once in the tick loop
immediately after Behavior.compute(), before the value is stored/served/
logged anywhere."""
from __future__ import annotations

from src.legacy import normalize_present_value


def test_binary_types_always_normalize_to_bool():
    for object_type in ("binary-input", "binary-output", "binary-value"):
        assert normalize_present_value(object_type, True) is True
        assert normalize_present_value(object_type, False) is False
        assert normalize_present_value(object_type, 1.0) is True
        assert normalize_present_value(object_type, 0.0) is False
        assert normalize_present_value(object_type, 1) is True
        assert normalize_present_value(object_type, 0) is False


def test_non_binary_types_pass_through_unchanged():
    assert normalize_present_value("analog-input", 1.0) == 1.0
    assert normalize_present_value("analog-value", 0.0) == 0.0
    assert normalize_present_value("multi-state-input", 3) == 3


def test_manual_behavior_numeric_input_still_normalizes_correctly():
    """ManualBehavior.compute() coerces numeric input (e.g. the raw '1.0'
    a user types into the old plain-number Set Value control) to float --
    normalize_present_value() must still map that back to the correct bool
    for a binary point, matching what the UI's "ON"/"OFF" label shows."""
    from src.legacy import ManualBehavior

    behavior = ManualBehavior({"value": 1.0})
    assert normalize_present_value("binary-input", behavior.compute(None)) is True

    behavior = ManualBehavior({"value": 0.0})
    assert normalize_present_value("binary-input", behavior.compute(None)) is False

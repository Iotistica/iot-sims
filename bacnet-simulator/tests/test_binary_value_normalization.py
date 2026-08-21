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

from src.bacnet.app import normalize_present_value


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
    from src.simulation.behaviors import ManualBehavior

    behavior = ManualBehavior({"value": 1.0})
    assert normalize_present_value("binary-input", behavior.compute(None)) is True

    behavior = ManualBehavior({"value": 0.0})
    assert normalize_present_value("binary-input", behavior.compute(None)) is False


def test_manual_behavior_accepts_on_off_style_strings():
    """Regression: a Functional Test Set block (or any manual-override
    write) sending the string "OFF" used to hit ManualBehavior's fallback
    `float(raw)` branch and crash with "could not convert string to float:
    'OFF'" -- only literal "true"/"false" were recognized as boolean-ish.
    This is this app's own displayed vocabulary for binary points (see
    admin/src/format.ts's formatPresentValue: ON/OFF), so it must be
    accepted, not just JSON true/false."""
    from src.simulation.behaviors import ManualBehavior

    for word, expected in [
        ("on", True), ("ON", True), (" On ", True), ("active", True), ("true", True),
        ("off", False), ("OFF", False), (" Off ", False), ("inactive", False), ("false", False),
    ]:
        behavior = ManualBehavior({"value": word})
        assert behavior.compute(None) is expected, f"{word!r} -> {behavior.compute(None)!r}"


def test_manual_behavior_set_also_coerces_on_off_strings():
    """set() (used by SimEngine.set_manual_value when the live behavior is
    already a ManualBehavior instance) must apply the same coercion as
    construction -- otherwise a restore/re-write after the object already
    has manual behavior would silently store the raw string instead of a
    proper bool."""
    from src.simulation.behaviors import ManualBehavior

    behavior = ManualBehavior({"value": True})
    behavior.set("off")
    assert behavior.compute(None) is False
    behavior.set("ON")
    assert behavior.compute(None) is True


def test_manual_behavior_numeric_strings_still_coerce_to_float():
    """Non-boolean-word strings (e.g. an analog manual override typed as
    text) must still fall through to float(), unchanged from before."""
    from src.simulation.behaviors import ManualBehavior

    assert ManualBehavior({"value": "72.5"}).compute(None) == 72.5


def test_coerce_binary_write_value():
    """write_priority()'s binary branch used to do a bare
    `bool(value) if not isinstance(value, bool) else value`, which is True
    for ANY non-empty string -- bool("0") and bool("off") were both True,
    so writing "0"/"off" to a binary-output silently turned it ON instead
    of OFF."""
    from src.bacnet.app import coerce_binary_write_value

    assert coerce_binary_write_value(True) is True
    assert coerce_binary_write_value(False) is False
    assert coerce_binary_write_value("on") is True
    assert coerce_binary_write_value("ON") is True
    assert coerce_binary_write_value("off") is False
    assert coerce_binary_write_value("OFF") is False
    assert coerce_binary_write_value("0") is False
    assert coerce_binary_write_value("1") is True
    assert coerce_binary_write_value(0) is False
    assert coerce_binary_write_value(1) is True

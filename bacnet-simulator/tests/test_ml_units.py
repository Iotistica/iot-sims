"""src/semantics/ml_units.py -- the one shared source->model-catalog-unit normalization
used by both the ML training-CSV builder and the live inference payload builder."""
from __future__ import annotations

import pytest

from src.semantics import ml_units as u


def test_fahrenheit_to_celsius_is_convertible_and_converts():
    assert u.compatibility("degrees-fahrenheit", "degC") == "convertible"
    assert u.normalize(55.4, "degrees-fahrenheit", "degC") == pytest.approx(13.0)
    assert u.normalize(32.0, "degrees-fahrenheit", "degC") == pytest.approx(0.0)


def test_same_unit_passes_through_unchanged():
    assert u.compatibility("degrees-celsius", "degC") == "same"
    assert u.normalize(13.0, "degrees-celsius", "degC") == 13.0
    assert u.normalize(13.0, "degC", "degC") == 13.0


def test_incompatible_dimension_raises():
    assert u.compatibility("percent", "degC") == "incompatible"
    with pytest.raises(u.UnitIncompatibleError):
        u.normalize(50.0, "percent", "degC")


def test_unknown_or_missing_source_unit_raises_when_conversion_needed():
    assert u.compatibility(None, "degC") == "unknown"
    assert u.compatibility("furlongs", "degC") == "unknown"
    with pytest.raises(u.UnitUnknownError):
        u.normalize(10.0, None, "degC")
    with pytest.raises(u.UnitUnknownError):
        u.normalize(10.0, "furlongs", "degC")


@pytest.mark.parametrize("value,src,exp,want", [
    (50.0, "percent", "%", 50.0),          # % is a passthrough token
    (0.5, "fraction", "percent", 50.0),
    (2.0, "kilowatts", "W", 2000.0),
    (2000.0, "watts", "kW", 2.0),
    (1.0, "cubic-feet-per-minute", "m3/s", pytest.approx(0.0004719, abs=1e-6)),
    (1.0, "liters-per-second", "m3/s", 0.001),
    (101325.0, "pascals", "kPa", pytest.approx(101.325)),
    (100.0, "degrees-kelvin", "degC", pytest.approx(-173.15)),
])
def test_common_project_conversions(value, src, exp, want):
    assert u.normalize(value, src, exp) == want


def test_passthrough_expected_units_never_convert():
    for exp in (None, "no-units", "1", "boolean"):
        assert u.normalize(7.0, "degrees-fahrenheit", exp) == 7.0
        assert u.compatibility("degrees-fahrenheit", exp) == "same"

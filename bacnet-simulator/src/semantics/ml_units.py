"""One shared engineering-unit normalization used by BOTH the ML training-CSV builder
(`src/simulation/training_export.py`) and the live inference-payload builder
(`src/simulation/providers/fmu.py`), so a model trained on a °C point and deployed against
a °F point ultimately receives the same number.

Scope and rules (no silent assumptions):

  * `expected_unit` is the **model catalog / API unit** the ML model was trained on
    (`VariableMetadata.unit`, e.g. ``degC``, ``m3/s``, ``percent``) -- NOT any FMU-native
    representation. `model.json` `conversion` (``c_to_k`` ...) is a *different*, later
    layer and is not touched here.
  * `expected_unit` is None / ``no-units`` / ``1`` / ``boolean``  -> value passes through.
  * `source_unit` canonicalizes equal to `expected_unit`         -> value unchanged.
  * same physical dimension (incl. °F <-> °C <-> K)               -> affine convert.
  * different dimension                                           -> `UnitIncompatibleError`.
  * `expected_unit` needs a real unit but `source_unit` is None / unrecognized
                                                                  -> `UnitUnknownError`.

There is deliberately no `pint` dependency and no learned/fuzzy matching -- every unit
spelling in play (the BACnet ``objects.units`` vocabulary and the shorter `model.json`
spellings) is in `_REGISTRY` below with an explicit (dimension, scale, offset).
"""
from __future__ import annotations

from typing import Literal

ML_UNIT_CONTRACT_VERSION = 1

_PASSTHROUGH = {None, "", "no-units", "nounits", "none", "1", "unitless", "boolean", "bool"}


class UnitError(ValueError):
    """Base for a unit problem that must stop training / block inference, never be guessed."""


class UnitIncompatibleError(UnitError):
    """source and expected units are different physical dimensions."""


class UnitUnknownError(UnitError):
    """a real conversion is required but the source unit is missing or unrecognized."""


def _canon(unit: str | None) -> str | None:
    if unit is None:
        return None
    return "".join(ch for ch in str(unit).strip().lower() if ch.isalnum())


# canonical-token -> (dimension, scale, offset): value_in_SI = raw * scale + offset.
# SI reference per dimension: K, fraction, Pa, W, J, m3/s, kg/s, m/s, lx, ppm, A, V,
# rad/s, s, W/m2.
_CFM_TO_M3S = 0.0004719474432
_LPS_TO_M3S = 0.001
_M3H_TO_M3S = 1.0 / 3600.0

_REGISTRY: dict[str, tuple[str, float, float]] = {
    # temperature (affine)
    "degc": ("temperature", 1.0, 273.15), "c": ("temperature", 1.0, 273.15),
    "degreescelsius": ("temperature", 1.0, 273.15), "celsius": ("temperature", 1.0, 273.15),
    "degf": ("temperature", 5.0 / 9.0, 273.15 - 32.0 * 5.0 / 9.0),
    "degreesfahrenheit": ("temperature", 5.0 / 9.0, 273.15 - 32.0 * 5.0 / 9.0),
    "fahrenheit": ("temperature", 5.0 / 9.0, 273.15 - 32.0 * 5.0 / 9.0),
    "k": ("temperature", 1.0, 0.0), "degk": ("temperature", 1.0, 0.0),
    "degreeskelvin": ("temperature", 1.0, 0.0), "kelvin": ("temperature", 1.0, 0.0),
    # ratio / percentage
    "percent": ("ratio", 0.01, 0.0), "pct": ("ratio", 0.01, 0.0),
    "fraction": ("ratio", 1.0, 0.0),
    # pressure
    "pa": ("pressure", 1.0, 0.0), "pascals": ("pressure", 1.0, 0.0),
    "kpa": ("pressure", 1000.0, 0.0), "kilopascals": ("pressure", 1000.0, 0.0),
    "bar": ("pressure", 1e5, 0.0), "bars": ("pressure", 1e5, 0.0),
    # power
    "w": ("power", 1.0, 0.0), "watts": ("power", 1.0, 0.0),
    "kw": ("power", 1000.0, 0.0), "kilowatts": ("power", 1000.0, 0.0),
    # energy
    "wh": ("energy", 3600.0, 0.0), "watthours": ("energy", 3600.0, 0.0),
    "kwh": ("energy", 3.6e6, 0.0), "kilowatthours": ("energy", 3.6e6, 0.0),
    # volumetric flow
    "m3s": ("volflow", 1.0, 0.0), "cubicmeterspersecond": ("volflow", 1.0, 0.0),
    "cfm": ("volflow", _CFM_TO_M3S, 0.0), "cubicfeetperminute": ("volflow", _CFM_TO_M3S, 0.0),
    "lps": ("volflow", _LPS_TO_M3S, 0.0), "literspersecond": ("volflow", _LPS_TO_M3S, 0.0),
    "m3h": ("volflow", _M3H_TO_M3S, 0.0), "cubicmetersperhour": ("volflow", _M3H_TO_M3S, 0.0),
    # mass flow
    "kgs": ("massflow", 1.0, 0.0), "kilogramspersecond": ("massflow", 1.0, 0.0),
    # velocity
    "ms": ("velocity", 1.0, 0.0), "meterspersecond": ("velocity", 1.0, 0.0),
    # illuminance
    "lx": ("illuminance", 1.0, 0.0), "luxes": ("illuminance", 1.0, 0.0), "lux": ("illuminance", 1.0, 0.0),
    # concentration
    "ppm": ("concentration", 1.0, 0.0), "partspermillion": ("concentration", 1.0, 0.0),
    # electrical
    "a": ("current", 1.0, 0.0), "amperes": ("current", 1.0, 0.0),
    "v": ("voltage", 1.0, 0.0), "volts": ("voltage", 1.0, 0.0),
    # angular speed
    "rpm": ("angularspeed", 1.0, 0.0), "revolutionsperminute": ("angularspeed", 1.0, 0.0),
    # time
    "s": ("time", 1.0, 0.0), "sec": ("time", 1.0, 0.0), "seconds": ("time", 1.0, 0.0),
    "h": ("time", 3600.0, 0.0), "hours": ("time", 3600.0, 0.0),
    # irradiance
    "wm2": ("irradiance", 1.0, 0.0),
}


def _lookup(unit: str | None) -> tuple[str, float, float] | None:
    return _REGISTRY.get(_canon(unit) or "")


def _is_passthrough(unit: str | None) -> bool:
    return _canon(unit) in _PASSTHROUGH or unit is None


def compatibility(
    source_unit: str | None, expected_unit: str | None
) -> Literal["same", "convertible", "incompatible", "unknown"]:
    """Classify without raising -- used by the target-device readiness check. A
    ``convertible`` pair is *fine* (informational), only ``incompatible`` / ``unknown``
    block."""
    if _is_passthrough(expected_unit):
        return "same"
    exp = _lookup(expected_unit)
    if exp is None:
        return "unknown"  # expected unit itself unrecognized -- treat like an unknown pair
    src = _lookup(source_unit)
    if src is None:
        return "unknown"
    if src == exp:  # same dimension + identical scale/offset -> the same unit (e.g. degC == degrees-celsius)
        return "same"
    if src[0] != exp[0]:
        return "incompatible"
    return "convertible"


def normalize(value: float, source_unit: str | None, expected_unit: str | None) -> float:
    """Convert `value` from `source_unit` into `expected_unit` (the model catalog unit).
    Raises `UnitIncompatibleError` / `UnitUnknownError` rather than guess."""
    v = float(value)
    if _is_passthrough(expected_unit):
        return v
    exp = _lookup(expected_unit)
    src = _lookup(source_unit)
    if exp is not None and src == exp:
        return v  # identical unit (possibly a different spelling)
    if exp is None:
        raise UnitUnknownError(
            f"expected unit {expected_unit!r} is not a recognized engineering unit"
        )
    if src is None:
        raise UnitUnknownError(
            f"cannot convert to {expected_unit!r}: source unit "
            f"{source_unit!r} is missing or not recognized"
        )
    if src[0] != exp[0]:
        raise UnitIncompatibleError(
            f"{source_unit!r} ({src[0]}) is not convertible to {expected_unit!r} ({exp[0]})"
        )
    _, s_scale, s_off = src
    _, e_scale, e_off = exp
    si = v * s_scale + s_off          # source -> SI
    return (si - e_off) / e_scale     # SI -> expected

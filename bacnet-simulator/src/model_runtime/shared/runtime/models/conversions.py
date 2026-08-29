from __future__ import annotations

from typing import Any


def scalar(value: Any) -> float:
    try:
        return float(value[-1])
    except (TypeError, IndexError):
        return float(value)


def apply_input_conversion(value: float, conversion: str | None) -> float:
    if conversion == "c_to_k":
        return value + 273.15
    if conversion == "pct_to_fraction":
        return value / 100.0
    if conversion == "lps_to_m3_s":
        return value / 1000.0
    return value


def apply_output_conversion(value: float, conversion: str | None) -> float:
    if conversion == "k_to_c":
        return value - 273.15
    if conversion == "fraction_to_pct":
        return value * 100.0
    if conversion == "w_to_kw":
        return value / 1000.0
    if conversion == "m3_s_to_lps":
        return value * 1000.0
    return value

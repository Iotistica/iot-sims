from __future__ import annotations

import math
from typing import Any

from .catalog import ModelMetadata


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _fmt(value: Any, unit: str | None = None) -> str:
    numeric = _number(value)
    if numeric is None:
        return str(value)
    suffix = unit or ""
    return f"{numeric:.2f}{suffix}"


def _short_name(name: str) -> str:
    replacements = {
        "heating_setpoint_c": "heat_sp",
        "cooling_setpoint_c": "cool_sp",
        "supply_air_temp_c": "sat",
        "outdoor_temp_c": "oat",
        "internal_gain_w": "gain",
        "zone_temp_c": "zone",
        "supply_airflow_m3_s": "flow",
        "discharge_air_temp_c": "dat",
        "damper_command_pct": "dam_cmd",
        "damper_position_pct": "dam_pos",
        "reheat_valve_command_pct": "reheat_cmd",
        "reheat_valve_position_pct": "reheat_pos",
        "supply_air_temp_setpoint_c": "sat_sp",
        "outdoor_air_temp_c": "oat",
        "return_air_temp_c": "rat",
        "fan_command_pct": "fan_cmd",
        "mixed_air_temp_c": "mat",
        "outdoor_air_damper_pct": "oa_damper",
        "heating_valve_command_pct": "heat_valve",
        "cooling_valve_command_pct": "cool_valve",
    }
    return replacements.get(name, name)


def _unit_suffix(unit: str | None) -> str:
    return {
        "degC": "C",
        "percent": "%",
        "m3/s": "m3/s",
        "W": "W",
    }.get(unit or "", unit or "")


def _values_line(values: dict[str, Any], units: dict[str, str | None]) -> str:
    return " ".join(
        f"{_short_name(name)}={_fmt(value, _unit_suffix(units.get(name)))}"
        for name, value in values.items()
    )


def _invalid_output_warnings(outputs: dict[str, Any], model: ModelMetadata) -> list[str]:
    warnings: list[str] = []
    expected = {item.name for item in model.outputs}
    for name in expected:
        if name not in outputs:
            warnings.append(f"missing_output:{name}")
            continue
        if _number(outputs[name]) is None:
            warnings.append(f"invalid_output:{name}")
    return warnings


def _delta_warnings(deltas: dict[str, float]) -> list[str]:
    warnings: list[str] = []
    for name, delta in deltas.items():
        abs_delta = abs(delta)
        if name.endswith("_temp_c") and abs_delta > 10:
            warnings.append(f"large_temperature_delta:{name}:{delta:+.2f}")
        elif name.endswith("_pct") and abs_delta > 50:
            warnings.append(f"large_percent_delta:{name}:{delta:+.2f}")
        elif name.endswith("_m3_s") and abs_delta > 5:
            warnings.append(f"large_flow_delta:{name}:{delta:+.2f}")
    return warnings


def _output_deltas(
    previous_outputs: dict[str, Any] | None,
    outputs: dict[str, Any],
) -> dict[str, float]:
    if not previous_outputs:
        return {}
    deltas: dict[str, float] = {}
    for name, value in outputs.items():
        current = _number(value)
        previous = _number(previous_outputs.get(name))
        if current is not None and previous is not None:
            deltas[name] = current - previous
    return deltas


def _vav_diagnostics(inputs: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    zone = _number(outputs.get("zone_temp_c"))
    heat_sp = _number(inputs.get("heating_setpoint_c"))
    cool_sp = _number(inputs.get("cooling_setpoint_c"))
    dam_cmd = _number(outputs.get("damper_command_pct"))
    dam_pos = _number(outputs.get("damper_position_pct"))
    reheat_cmd = _number(outputs.get("reheat_valve_command_pct"))
    reheat_pos = _number(outputs.get("reheat_valve_position_pct"))
    warnings: list[str] = []

    heat_error = heat_sp - zone if heat_sp is not None and zone is not None else None
    cool_error = cool_sp - zone if cool_sp is not None and zone is not None else None
    mode = "UNKNOWN"
    if heat_error is not None and heat_error > 0.25:
        mode = "HEATING"
    elif cool_error is not None and cool_error < -0.25:
        mode = "COOLING"
    elif heat_error is not None and cool_error is not None:
        mode = "DEADBAND"

    if zone is not None and heat_sp is not None and reheat_cmd is not None:
        if zone > heat_sp + 0.25 and reheat_cmd > 50:
            warnings.append("reheat_active_above_heating_sp")
    if zone is not None and cool_sp is not None and reheat_cmd is not None:
        if zone > cool_sp + 0.25 and reheat_cmd > 5:
            warnings.append("reheat_active_above_cooling_sp")
    if dam_cmd is not None and reheat_cmd is not None and dam_cmd > 50 and reheat_cmd > 50:
        warnings.append("heating_and_cooling_active")
    if dam_cmd is not None and dam_pos is not None and abs(dam_cmd - dam_pos) > 15:
        warnings.append("damper_command_position_mismatch")
    if reheat_cmd is not None and reheat_pos is not None and abs(reheat_cmd - reheat_pos) > 15:
        warnings.append("reheat_command_position_mismatch")

    return {
        "mode": mode,
        "heating_setpoint_error_c": heat_error,
        "cooling_setpoint_error_c": cool_error,
        "damper_command_position_delta_pct": (
            dam_cmd - dam_pos if dam_cmd is not None and dam_pos is not None else None
        ),
        "reheat_command_position_delta_pct": (
            reheat_cmd - reheat_pos if reheat_cmd is not None and reheat_pos is not None else None
        ),
        "warnings": warnings,
    }


def _ahu_diagnostics(inputs: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    sat_sp = _number(inputs.get("supply_air_temp_setpoint_c"))
    sat = _number(outputs.get("supply_air_temp_c"))
    heat = _number(outputs.get("heating_valve_command_pct"))
    cool = _number(outputs.get("cooling_valve_command_pct"))
    fan = _number(outputs.get("fan_command_pct"))
    oa_damper = _number(outputs.get("outdoor_air_damper_pct"))
    warnings: list[str] = []

    if heat is not None and cool is not None and heat > 10 and cool > 10:
        warnings.append("simultaneous_heating_and_cooling")
    if fan is not None and (fan < -0.1 or fan > 100.1):
        warnings.append("fan_command_out_of_range")
    if oa_damper is not None and (oa_damper < -0.1 or oa_damper > 100.1):
        warnings.append("oa_damper_out_of_range")

    return {
        "supply_air_temp_setpoint_error_c": (
            sat_sp - sat if sat_sp is not None and sat is not None else None
        ),
        "heating_valve_command_pct": heat,
        "cooling_valve_command_pct": cool,
        "fan_command_pct": fan,
        "outdoor_air_damper_pct": oa_damper,
        "mixed_air_temp_c": outputs.get("mixed_air_temp_c"),
        "supply_air_temp_c": outputs.get("supply_air_temp_c"),
        "warnings": warnings,
    }


def build_step_diagnostics(
    *,
    model: ModelMetadata,
    session_id: str,
    start_time: float,
    final_time: float,
    time_step: float,
    api_inputs: dict[str, Any],
    fmu_inputs: dict[str, dict[str, Any]],
    outputs: dict[str, Any],
    previous_outputs: dict[str, Any] | None,
    elapsed_ms: float,
    solver_log_tail: str = "",
) -> dict[str, Any]:
    api_units = {item.name: item.unit for item in model.inputs}
    output_units = {item.name: item.unit for item in model.outputs}
    deltas = _output_deltas(previous_outputs, outputs)
    warnings = [
        *_invalid_output_warnings(outputs, model),
        *_delta_warnings(deltas),
    ]

    model_diag: dict[str, Any] = {}
    if model.slug == "SimpleVAVZone":
        model_diag = _vav_diagnostics(api_inputs, outputs)
    elif model.slug == "SimpleAHU":
        model_diag = _ahu_diagnostics(api_inputs, outputs)
    warnings.extend(model_diag.get("warnings") or [])

    return {
        "model_id": model.id,
        "model_slug": model.slug,
        "session_id": session_id,
        "current_time": start_time,
        "target_time": final_time,
        "time_step": time_step,
        "api_inputs": dict(api_inputs),
        "fmu_inputs": dict(fmu_inputs),
        "outputs": dict(outputs),
        "output_deltas": deltas,
        "diagnostics": {**model_diag, "warnings": warnings},
        "solver": {
            "elapsed_ms": elapsed_ms,
            "log_tail": solver_log_tail,
        },
        "log_lines": {
            "step": (
                f"[FMU STEP] model={model.slug} session={session_id} "
                f"t={start_time:g}->{final_time:g} dt={time_step:g}"
            ),
            "api_inputs": f"INPUT api: {_values_line(api_inputs, api_units)}",
            "fmu_inputs": "INPUT fmu: "
            + " ".join(
                f"{_short_name(name)}={_fmt(detail.get('value'), _unit_suffix(detail.get('fmu_unit')))}"
                for name, detail in fmu_inputs.items()
            ),
            "outputs": f"OUTPUT: {_values_line(outputs, output_units)}",
            "diagnostics": "DIAG: "
            + " ".join(
                f"{key}={_fmt(value, 'C' if key.endswith('_c') else None)}"
                for key, value in model_diag.items()
                if key != "warnings" and value is not None
            )
            + (f" WARN={','.join(warnings)}" if warnings else ""),
        },
    }


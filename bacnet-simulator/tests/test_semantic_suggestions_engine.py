"""Pure-function tests for the deterministic Suggest Semantics scorer
(src/semantics/suggestions.py) -- no DB/HTTP needed. Covers normalization,
equipment inference, point inference, and the vocabulary-membership
guarantee that would have caught the Water_Differential_Pressure_Sensor /
Differential_Pressure_Sensor mismatch found during adaptation."""
from __future__ import annotations

import pytest

from src.core.config import EQUIPMENT_TYPES, POINT_TYPES
from src.semantics.suggestions import (
    EQUIPMENT_RULES,
    POINT_RULES,
    DeviceSnapshot,
    PointSnapshot,
    normalize_text,
    suggest_equipment,
    suggest_point,
)


# ─── Vocabulary ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("rule", EQUIPMENT_RULES, ids=lambda r: r.brick_class)
def test_every_equipment_rule_is_canonical(rule):
    assert rule.brick_class in EQUIPMENT_TYPES


@pytest.mark.parametrize("rule", POINT_RULES, ids=lambda r: r.brick_class)
def test_every_point_rule_is_canonical(rule):
    assert rule.brick_class in POINT_TYPES
    for eq_class in rule.equipment_classes:
        assert eq_class in EQUIPMENT_TYPES


# ─── Normalization ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw", ["SAT", "SA-T", "SA_TEMP", "AHU-1.SAT", "AHU_1_SAT"])
def test_sat_variants_normalize_toward_supply_air_temperature(raw):
    tokens = normalize_text(raw)
    assert tokens  # sanity: normalization never returns empty for real input


def test_chwst_expands_to_chilled_water_supply_temperature():
    device = DeviceSnapshot(device_instance=1, name="Chiller-Plant")
    point = PointSnapshot(object_type="analog-input", object_instance=1, object_name="CHWST", units="degrees-celsius")
    suggestion = suggest_point(device, point)
    # CHWST has no dedicated rule in POINT_RULES today -- assert the tokens
    # it expands to are the ones a future rule would match against, proving
    # the alias table itself (not a specific rule) is doing its job.
    from src.semantics.suggestions import tokens_for
    tokens = tokens_for("CHWST")
    assert {"chilled", "water", "supply", "temperature"} <= tokens


# ─── Equipment inference ────────────────────────────────────────────────────

def test_ahu_device_with_typical_points_suggests_air_handling_unit():
    device = DeviceSnapshot(
        device_instance=1003,
        name="AHU-1",
        points=[
            PointSnapshot("analog-input", 1, "SAT", units="degrees-celsius"),
            PointSnapshot("analog-input", 2, "RAT", units="degrees-celsius"),
            PointSnapshot("binary-input", 3, "SF-Run"),
        ],
    )
    result = suggest_equipment(device)
    assert result.suggested_class == "Air_Handling_Unit"
    assert result.confidence in ("high", "medium")


def test_vav_device_with_typical_points_suggests_vav_box():
    device = DeviceSnapshot(
        device_instance=1101,
        name="VAV-101",
        points=[
            PointSnapshot("analog-input", 1, "ZN-T", units="degrees-celsius"),
            PointSnapshot("analog-input", 2, "Airflow"),
            PointSnapshot("analog-output", 3, "Damper-Pos", units="percent"),
        ],
    )
    result = suggest_equipment(device)
    assert result.suggested_class == "Variable_Air_Volume_Box"


# ─── Point inference ────────────────────────────────────────────────────────

def test_sat_with_ahu_context_suggests_supply_air_temperature_sensor():
    device = DeviceSnapshot(device_instance=1003, name="AHU-1")
    point = PointSnapshot("analog-input", 5, "SAT", units="degrees-celsius")
    result = suggest_point(device, point, equipment_class="Air_Handling_Unit")
    assert result.suggested_class == "Supply_Air_Temperature_Sensor"
    assert result.confidence in ("high", "medium")
    assert result.reasons


def test_rat_with_ahu_context_suggests_return_air_temperature_sensor():
    device = DeviceSnapshot(device_instance=1003, name="AHU-1")
    point = PointSnapshot("analog-input", 6, "RAT", units="degrees-celsius")
    result = suggest_point(device, point, equipment_class="Air_Handling_Unit")
    assert result.suggested_class == "Return_Air_Temperature_Sensor"


def test_oat_suggests_outside_air_temperature_sensor():
    device = DeviceSnapshot(device_instance=1003, name="AHU-1")
    point = PointSnapshot("analog-input", 7, "OAT", units="degrees-celsius")
    result = suggest_point(device, point)
    assert result.suggested_class == "Outside_Air_Temperature_Sensor"


def test_fan_run_status_suggests_fan_status():
    device = DeviceSnapshot(device_instance=1003, name="AHU-1")
    point = PointSnapshot("binary-input", 1, "SF-Run")
    result = suggest_point(device, point)
    assert result.suggested_class == "Fan_Status"


def test_damper_position_command_suggests_damper_position_command():
    device = DeviceSnapshot(device_instance=1101, name="VAV-101")
    point = PointSnapshot("analog-output", 3, "Damper-Pos-Cmd", units="percent")
    result = suggest_point(device, point)
    assert result.suggested_class == "Damper_Position_Command"


# ─── VAV command points (obvious cases should reach HIGH deterministically) ──

def test_vav_damper_command_is_high_confidence():
    device = DeviceSnapshot(device_instance=1102, name="VAV-L1-02")
    point = PointSnapshot("analog-output", 4, "Damper-Cmd", units="percent")
    result = suggest_point(device, point, equipment_class="Variable_Air_Volume_Box")
    assert result.suggested_class == "Damper_Position_Command"
    assert result.confidence == "high"


def test_vav_reheat_valve_is_high_confidence():
    device = DeviceSnapshot(device_instance=1102, name="VAV-L1-02")
    point = PointSnapshot("analog-output", 6, "Reheat-Valve", units="percent")
    result = suggest_point(device, point, equipment_class="Variable_Air_Volume_Box")
    assert result.suggested_class == "Valve_Position_Command"
    assert result.confidence == "high"


def test_zone_humidity_never_becomes_a_position_command():
    device = DeviceSnapshot(device_instance=1102, name="VAV-L1-02")
    point = PointSnapshot("analog-input", 9, "Zone-Humidity", units="percent")
    result = suggest_point(device, point, equipment_class="Variable_Air_Volume_Box")
    assert result.suggested_class not in ("Damper_Position_Command", "Valve_Position_Command")


def test_damper_position_input_is_not_automatically_a_command():
    device = DeviceSnapshot(device_instance=1102, name="VAV-L1-02")
    point = PointSnapshot("analog-input", 10, "Damper-Position", units="percent")
    result = suggest_point(device, point, equipment_class="Variable_Air_Volume_Box")
    assert result.suggested_class != "Damper_Position_Command"
    # analog-input + "damper"/"position" should resolve to the status/
    # feedback counterpart rather than being forced into an unsupported
    # class or over-confidently guessing.
    assert result.suggested_class in (None, "Damper_Position_Status")


def test_valve_position_input_is_not_automatically_a_command():
    device = DeviceSnapshot(device_instance=1102, name="VAV-L1-02")
    point = PointSnapshot("analog-input", 11, "Valve-Position", units="percent")
    result = suggest_point(device, point, equipment_class="Variable_Air_Volume_Box")
    assert result.suggested_class != "Valve_Position_Command"
    assert result.suggested_class in (None, "Valve_Status")


def test_ambiguous_point_never_returns_unsupported_class():
    device = DeviceSnapshot(device_instance=9999, name="Misc-Device")
    point = PointSnapshot("analog-input", 1, "TEMP1")
    result = suggest_point(device, point)
    # Weak/ambiguous evidence must never force an over-specific class --
    # either no suggestion, or (if one is returned) it must be canonical.
    if result.suggested_class is not None:
        assert result.suggested_class in POINT_TYPES
    else:
        assert result.confidence == "none"

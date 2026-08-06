"""Static configuration: env-derived paths/ports, validation enums, and JWT
secret resolution. No behavior here — just constants and the one small
function needed to resolve the JWT signing secret at import time.

Part of the src package — extracted from bacnet_simulator.py per GH #15
(Refactor bacnet_simulator.py), pass 1 — moved verbatim, no logic changes.
"""
import os
import secrets
from pathlib import Path
from typing import Optional

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "bacnet_sim.db"
SIM_API_PORT = int(os.environ.get("SIM_API_PORT", "47900"))
BACNET_PORT = int(os.environ.get("BACNET_PORT", "47808"))

VALID_OBJECT_TYPES = {
    "analog-input", "analog-output", "analog-value",
    "binary-input", "binary-output", "binary-value",
    "multi-state-input", "multi-state-output", "multi-state-value",
}
# Calendar (GH #18) is NOT part of this set — like Trend Log and Schedule, it
# has its own DB table/CRUD/drawer rather than being shoehorned into the
# generic objects table, since it has no units/behavior/number_of_states.
MULTISTATE_TYPES = {"multi-state-input", "multi-state-output", "multi-state-value"}

# Object types that are actually Commandable (real 16-slot priorityArray) in
# this bacpypes3 version (GH #17) — note *-value objects are NOT Commandable
# here despite being writable, so they have no priority array to expose.
COMMANDABLE_TYPES = {"analog-output", "binary-output", "multi-state-output"}

# Narrow, practical subset of BACnet's Reliability enum (GH #16) — enough to
# exercise client-side fault handling without modeling every standard value.
VALID_RELIABILITY = {
    "no-fault-detected", "no-sensor", "over-range", "under-range",
    "open-loop", "shorted-loop", "unreliable-other", "multi-state-fault",
}

# Binary Input/Output Polarity (GH #19) — Binary Value has no such property
# in bacpypes3's schema (matches real BACnet spec), so this only applies when
# object_type is binary-input or binary-output.
VALID_POLARITY = {"normal", "reverse"}

VALID_BEHAVIORS = {"constant", "sine", "noise", "random_walk", "manual", "schedule", "ramp", "fault"}

VALID_SEGMENTATION = {"segmented-both", "segmented-transmit", "segmented-receive", "no-segmentation"}

# ── Brick/Haystack-style semantic metadata (optional, additive) ────────────
# Purely descriptive vocabulary — never consulted by the BACnet protocol or
# simulation engine. Kept here alongside the other admin-UI-facing enums
# (exposed via GET /meta) so the frontend dropdowns and the validate_semantic()
# methods in schemas.py share one source of truth, same pattern as
# VALID_OBJECT_TYPES etc. above.
#
# Every key below is a real Brick class name, verified against the pinned
# release (not derived by naming convention — e.g. "AHU"/"VAV" are NOT real
# Brick class keys, the canonical names are Air_Handling_Unit/
# Variable_Air_Volume_Box, with AHU/VAV as Brick-defined aliases; several
# similarly-plausible-looking guesses like Freeze_Alarm/Filter_Alarm don't
# exist at all). Any future addition must be verified the same way: fetch
# https://github.com/BrickSchema/Brick/blob/{BRICK_VERSION-as-vNNN-tag}/bricksrc/<file>.py
# and confirm the exact key exists there — don't type one in by pattern.
BRICK_VERSION = "1.4.4"

EQUIPMENT_TYPES = {
    "Air_Handling_Unit": "AHU",
    "Variable_Air_Volume_Box": "VAV",
    "Boiler": "Boiler",
    "Chiller": "Chiller",
    "Cooling_Tower": "Cooling Tower",
    "Pump": "Pump",
    "Meter": "Meter",
    "Lighting_Equipment": "Lighting Equipment",
}

LOCATION_KINDS = {
    "Site": "Site",
    "Building": "Building",
    "Floor": "Floor",
    "Room": "Room",
    "Zone": "Zone",
}

POINT_TYPES = {
    # Sensors (bricksrc/sensor.py)
    "Supply_Air_Temperature_Sensor": "Supply Air Temperature",
    "Supply_Fan_Command": "Supply Fan Command",
    "Supply_Fan_Status": "Supply Fan Status",
    "Supply_Air_Temperature_Setpoint": (
        "Supply Air Temperature Setpoint"
    ),
    "Return_Air_Temperature_Sensor": "Return Air Temperature",
    "Mixed_Air_Temperature_Sensor": "Mixed Air Temperature",
    "Outside_Air_Temperature_Sensor": "Outside Air Temperature",
    "Zone_Air_Temperature_Sensor": "Zone Air Temperature",
    "Leaving_Chilled_Water_Temperature_Sensor": "Chilled Water Supply Temperature",
    "Entering_Chilled_Water_Temperature_Sensor": "Chilled Water Return Temperature",
    "Leaving_Hot_Water_Temperature_Sensor": "Hot Water Supply Temperature",
    "Entering_Hot_Water_Temperature_Sensor": "Hot Water Return Temperature",
    "Condenser_Water_Temperature_Sensor": "Condenser Water Temperature",
    "Temperature_Sensor": "Temperature",
    "Outside_Air_Humidity_Sensor": "Outside Air Humidity",
    "Zone_Air_Humidity_Sensor": "Zone Air Humidity",
    "CO2_Level_Sensor": "CO2 Level",
    "Occupancy_Sensor": "Occupancy",
    "Illuminance_Sensor": "Illuminance",
    "Air_Flow_Sensor": "Air Flow",
    "Static_Pressure_Sensor": "Static Pressure",
    "Differential_Pressure_Sensor": "Differential Pressure",
    "Water_Flow_Sensor": "Water Flow",
    "Water_Differential_Pressure_Sensor": "Water Differential Pressure",
    "Power_Sensor": "Power",
    "Energy_Sensor": "Energy",
    "Demand_Sensor": "Demand",
    "Peak_Demand_Sensor": "Peak Demand",
    "Voltage_Sensor": "Voltage",
    "Current_Sensor": "Current",
    "Power_Factor_Sensor": "Power Factor",
    "Frequency_Sensor": "Frequency",
    "Natural_Gas_Flow_Sensor": "Natural Gas Flow",
    # Setpoints (bricksrc/setpoint.py)
    "Supply_Air_Temperature_Setpoint": "Supply Air Temperature Setpoint",
    "Room_Air_Temperature_Setpoint": "Zone Air Temperature Setpoint",
    "Cooling_Temperature_Setpoint": "Cooling Setpoint",
    "Heating_Temperature_Setpoint": "Heating Setpoint",
    "Air_Flow_Setpoint": "Air Flow Setpoint",
    "Static_Pressure_Setpoint": "Static Pressure Setpoint",
    # Commands (bricksrc/command.py)
    "Damper_Position_Command": "Damper Position Command",
    "Valve_Position_Command": "Valve Position Command",
    "Fan_Speed_Command": "Fan Speed Command",
    "Speed_Command": "Speed Command",
    "Start_Stop_Command": "Start/Stop Command",
    "On_Off_Command": "On/Off Command",
    "Lighting_Level_Command": "Lighting Level Command",
    "Cooling_Command": "Cooling Command",
    "Heating_Command": "Heating Command",
    # Statuses (bricksrc/status.py)
    "Run_Status": "Run Status",
    "Fan_Status": "Fan Status",
    "Filter_Status": "Filter Status",
    "Occupancy_Status": "Occupancy Status",
    "Damper_Position_Status": "Damper Position Status",
    "Valve_Status": "Valve Status",
    "Enable_Status": "Enable Status",
    "Freeze_Status": "Freeze Status",
    # Alarms (bricksrc/alarm.py)
    "Alarm": "Alarm",
    "Change_Filter_Alarm": "Change Filter Alarm",
    "High_Temperature_Alarm": "High Temperature Alarm",
    "Low_Temperature_Alarm": "Low Temperature Alarm",
}

BACNET_UNITS = [
    "no-units", "degrees-celsius", "degrees-fahrenheit", "degrees-kelvin",
    "percent", "parts-per-million", "kilowatts", "watts", "kilowatt-hours",
    "amperes", "volts", "cubic-feet-per-minute", "liters-per-second",
    "pascals", "kilopascals", "bars", "cubic-meters-per-hour",
    "revolutions-per-minute", "meters-per-second", "luxes",
]

JWT_ALGORITHM = "HS256"
JWT_EXPIRE_HOURS = int(os.environ.get("JWT_EXPIRE_HOURS", "24"))
_JWT_SECRET_FILE = DATA_DIR / ".jwt_secret"
_jwt_secret_cache: Optional[str] = None


def _get_jwt_secret() -> str:
    """Resolve the JWT signing secret: env override, else a persisted random
    value in DATA_DIR so tokens survive process restarts."""
    global _jwt_secret_cache
    if _jwt_secret_cache is not None:
        return _jwt_secret_cache
    env_secret = os.environ.get("JWT_SECRET")
    if env_secret:
        _jwt_secret_cache = env_secret
        return _jwt_secret_cache
    _JWT_SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
    if _JWT_SECRET_FILE.exists():
        _jwt_secret_cache = _JWT_SECRET_FILE.read_text().strip()
    else:
        _jwt_secret_cache = secrets.token_hex(32)
        _JWT_SECRET_FILE.write_text(_jwt_secret_cache)
    return _jwt_secret_cache


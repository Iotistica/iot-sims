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

# Present_Value for these three object types is BACnetBinaryPV -- an
# ENUMERATED{inactive(0), active(1)}, not a float and not a general integer
# (ASHRAE 135 object type tables). The simulator's internal canonical
# representation is a plain Python bool (True=active, False=inactive) —
# see the coercion in SimEngine's tick loop (src/legacy.py) right after
# `val = behavior.compute(...)`, which is the single place a Behavior's
# possibly-float output gets normalized before being stored/served/logged.
BINARY_TYPES = {"binary-input", "binary-output", "binary-value"}

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

# ── Brick semantic metadata ──────────────────────────────────────────────
# Brick is the semantic source of truth (see src/semantics/mirror.py).
# EQUIPMENT_TYPES/POINT_TYPES/LOCATION_KINDS are the SAME canonical Brick
# vocabulary used for both semantic_entities.brick_class AND the legacy
# devices.equipment_type/objects.point_type/locations.kind compatibility
# mirror columns -- there is only one vocabulary, not two. Purely
# descriptive -- never consulted by the BACnet protocol or simulation
# engine. Kept here alongside the other admin-UI-facing enums (exposed via
# GET /meta) so the frontend dropdowns and the validate_semantic()
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

    # Brick Core (verified against bricksrc/equipment.py at v1.4.4):
    # Supply_Fan/Return_Fan are real subclasses of the real Fan class.
    # Used to represent an AHU's supply/return fans as their own semantic
    # sub-equipment entities (isPartOf the AHU), so their Fan_Status/
    # Fan_Speed_Command points can be disambiguated by which fan entity
    # they're isPointOf instead of by a separate point-type name per fan
    # -- see src/semantics/resolver.py.
    "Supply_Fan": "Supply Fan",
    "Return_Fan": "Return Fan",
    "Fan": "Fan",
}

LOCATION_KINDS = {
    "Site": "Site",
    "Building": "Building",
    "Floor": "Floor",
    "Room": "Room",
    "Zone": "Zone",

    # Brick Core (verified against bricksrc/location.py at v1.4.4):
    # Lighting_Zone is a real subclass of the real Zone location class --
    # NOT an equipment class, despite grouping DALI lighting points.
    # Brick's hasPoint/isPointOf relationship domain includes both
    # Equipment and Location, so a Lighting_Zone can directly host its
    # Power_Sensor/On_Off_Command/Lighting_Level_Command points.
    "Lighting_Zone": "Lighting Zone",
}

# Brick Core (verified against bricksrc/equipment.py at v1.4.4 -- already
# present at the currently-pinned BRICK_VERSION, no version change needed
# for the class itself): Controller -> ICT_Equipment -> Equipment. This is
# the semantic role a `devices` row may explicitly be given (see
# src/semantics/mirror.py's sync_controller_entity and the dedicated
# POST /devices/{id}/controller endpoint) -- deliberately NOT merged into
# EQUIPMENT_TYPES, since Equipment (the new `equipment` table) and
# Controller (the `devices` table) are now separate concepts with separate
# pickers. Only the generic class is listed; its real Brick subclasses
# (BACnet_Controller, Modbus_Controller) aren't needed at this vocabulary's
# current scope.
CONTROLLER_TYPES = {
    "Controller": "Controller",
}

# Brick Core semantic relationship predicates (see semantic_entities/
# semantic_relationships in src/legacy.py's Database.setup() and
# src/semantics/). Verified against the pinned v1.4.4 relationships.py:
# isPointOf/hasPoint, isPartOf/hasPart, feeds/isFedBy, hasLocation/
# isLocationOf are all real inverse-predicate pairs. Only the forward
# direction is stored; brick_export.py emits both directions on export.
#
# Storage-direction rule (apply this to any future predicate addition):
# always store whichever named predicate's real Brick rdfs:domain matches
# the entity being stored as source_entity_id -- isPointOf stores Point
# first because Point IS isPointOf's real domain; hasLocation stores
# Equipment first because Equipment IS hasLocation's real domain.
#
# controls/isHostedBy: verified against bricksrc/relationships.py at the
# real tag v1.5.0-rc1 specifically -- NOT present at the pinned
# BRICK_VERSION=1.4.4 (checked both tags directly). This is a narrow,
# isolated addition for Controller modeling only, not a project-wide Brick
# version bump -- EQUIPMENT_TYPES/POINT_TYPES/LOCATION_KINDS and the four
# predicates above remain verified only against 1.4.4. Re-verify/consolidate
# into BRICK_VERSION once Brick 1.5 has a stable (non-RC) tagged release.
#   controls:    domain=Controller,            range=Equipment,  inverse=isControlledBy
#   isHostedBy:  domain=Point,                 range=Controller (ICT_Equipment), inverse of hosts (domain=Controller)
SEMANTIC_PREDICATES = {
    "isPointOf": "Is Point Of",
    "isPartOf": "Is Part Of",
    "feeds": "Feeds",
    "hasLocation": "Has Location",
    "controls": "Controls",
    "isHostedBy": "Is Hosted By",
}

POINT_TYPES = {
    # ── Sensors ──────────────────────────────────────────────────────────────

    "Supply_Air_Temperature_Sensor": "Supply Air Temperature",
    "Return_Air_Temperature_Sensor": "Return Air Temperature",
    "Mixed_Air_Temperature_Sensor": "Mixed Air Temperature",
    "Outside_Air_Temperature_Sensor": "Outside Air Temperature",
    "Zone_Air_Temperature_Sensor": "Zone Air Temperature",

    # More specific AHU airflow sensor.
    "Supply_Air_Flow_Sensor": "Supply Air Flow",
    "Air_Flow_Sensor": "Air Flow",

    "Static_Pressure_Sensor": "Static Pressure",
    "Differential_Pressure_Sensor": "Differential Pressure",

    "Leaving_Chilled_Water_Temperature_Sensor":
        "Chilled Water Supply Temperature",
    "Entering_Chilled_Water_Temperature_Sensor":
        "Chilled Water Return Temperature",

    "Leaving_Hot_Water_Temperature_Sensor":
        "Hot Water Supply Temperature",
    "Entering_Hot_Water_Temperature_Sensor":
        "Hot Water Return Temperature",

    "Condenser_Water_Temperature_Sensor":
        "Condenser Water Temperature",

    "Temperature_Sensor": "Temperature",

    "Outside_Air_Humidity_Sensor": "Outside Air Humidity",
    "Zone_Air_Humidity_Sensor": "Zone Air Humidity",
    "CO2_Level_Sensor": "CO2 Level",
    "Occupancy_Sensor": "Occupancy",
    "Illuminance_Sensor": "Illuminance",

    "Water_Flow_Sensor": "Water Flow",
    "Water_Differential_Pressure_Sensor":
        "Water Differential Pressure",

    "Power_Sensor": "Power",
    "Energy_Sensor": "Energy",
    "Demand_Sensor": "Demand",
    "Peak_Demand_Sensor": "Peak Demand",
    "Voltage_Sensor": "Voltage",
    "Current_Sensor": "Current",
    "Power_Factor_Sensor": "Power Factor",
    "Frequency_Sensor": "Frequency",
    "Natural_Gas_Flow_Sensor": "Natural Gas Flow",

    # ── Setpoints ────────────────────────────────────────────────────────────

    "Supply_Air_Temperature_Setpoint":
        "Supply Air Temperature Setpoint",

    "Room_Air_Temperature_Setpoint":
        "Zone Air Temperature Setpoint",

    "Cooling_Temperature_Setpoint":
        "Cooling Setpoint",

    "Heating_Temperature_Setpoint":
        "Heating Setpoint",

    "Air_Flow_Setpoint": "Air Flow Setpoint",
    "Static_Pressure_Setpoint": "Static Pressure Setpoint",

    # ── Commands ─────────────────────────────────────────────────────────────

    "Supply_Fan_Command": "Supply Fan Command",

    # This is the standard Brick fan VFD/speed command.
    "Fan_Speed_Command": "Fan Speed Command",

    # Keep this because some generic motors/drives may use it.
    "Speed_Command": "Speed Command",

    "Damper_Position_Command":
        "Damper Position Command",

    "Valve_Position_Command":
        "Valve Position Command",

    "Start_Stop_Command": "Start/Stop Command",
    "On_Off_Command": "On/Off Command",

    "Lighting_Level_Command":
        "Lighting Level Command",

    "Cooling_Command": "Cooling Command",
    "Heating_Command": "Heating Command",

    # ── Statuses ─────────────────────────────────────────────────────────────

    # Generic fan status -- used for BOTH supply and return fans, each as
    # its own Supply_Fan/Return_Fan equipment entity (isPartOf the AHU),
    # disambiguated via isPointOf to the correct fan entity rather than a
    # separate Supply_Fan_Status/Return_Fan_Status alias class name (see
    # the removed-aliases note below and migrate_ahu_fan_aliases() in
    # src/semantics/backfill.py, which migrates any object still tagged
    # with the old Supply_Fan_Status alias to this canonical class).
    "Fan_Status": "Fan Status",

    "Run_Status": "Run Status",

    "Filter_Status": "Filter Status",
    "Occupancy_Status": "Occupancy Status",

    "Damper_Position_Status":
        "Damper Position Status",

    "Valve_Status": "Valve Status",
    "Enable_Status": "Enable Status",
    "Freeze_Status": "Freeze Status",

    # ── Alarms ───────────────────────────────────────────────────────────────

    "Alarm": "Alarm",
    "Change_Filter_Alarm": "Change Filter Alarm",
    "High_Temperature_Alarm": "High Temperature Alarm",
    "Low_Temperature_Alarm": "Low Temperature Alarm",

    # The temporary non-canonical AHU fan aliases -- Supply_Fan_Speed_Command,
    # Return_Fan_Speed_Command, Supply_Fan_Status, and a duplicate
    # re-definition of Return_Fan_Status -- that used to live here have all
    # been removed by the Brick Core migration. Fan_Status/Fan_Speed_Command
    # above are now the only fan point classes needed, disambiguated per
    # instance via isPointOf to a Supply_Fan vs Return_Fan equipment entity
    # rather than via separate alias class names. This is enforced going
    # forward (validate_semantic_entity/ObjectCreate.validate_semantic()
    # reject these values for NEW objects), and existing databases are
    # actively migrated -- not just tolerated -- by
    # migrate_ahu_fan_aliases() (src/semantics/backfill.py), which runs on
    # every Database.setup() and rewrites any object still carrying one of
    # these four alias strings to the canonical class, building the
    # matching Supply_Fan/Return_Fan sub-equipment entity + isPartOf +
    # isPointOf relationships in the same pass -- exactly what
    # seed_default() creates for a fresh AHU. src/energy/context.py's
    # legacy per-field fallback (see src/semantics/resolver.py) remains as
    # defense-in-depth for the brief window before that migration first
    # runs, not as the primary compatibility mechanism.

    #-- Boiler -------------------------------------

    "Natural_Gas_Flow_Sensor": "Natural Gas Flow",
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


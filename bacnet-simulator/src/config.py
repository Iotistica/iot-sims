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

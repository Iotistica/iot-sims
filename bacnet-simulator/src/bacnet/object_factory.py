"""BACnet object construction boundary."""
from typing import Any

def create_object(engine: Any, row: dict, slot: int, device_name: str = ""):
    return engine._create_object(row, slot, device_name)

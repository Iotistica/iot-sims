"""Configured-to-wire BACnet object identity helpers."""
from typing import Optional

WIRE_SLOT_SIZE = 1000


def make_wire_instance(slot: int, configured_instance: int) -> int:
    return slot * WIRE_SLOT_SIZE + configured_instance


def split_wire_instance(wire_instance: int) -> tuple[int, int]:
    return divmod(wire_instance, WIRE_SLOT_SIZE)


def resolve_wire_object(engine, object_type: str, physical_instance: int) -> Optional[dict]:
    return engine.resolve_wire_object(object_type, physical_instance)

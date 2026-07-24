"""
Type definitions for the OPC UA simulator's live Device -> Tag hierarchy.

LiveDevice = one OPC UA folder node (+ its DeviceUUID child) — mirrors a row
in the `devices` SQLite table (see lib/db.py).
LiveTag = one OPC UA variable node + its running Behavior instance — mirrors
a row in the `tags` table, owned by a LiveDevice.

Kept distinct from the DB row dicts on purpose: these hold live asyncua Node
references and in-memory Behavior state that don't belong in SQLite.
"""
from dataclasses import dataclass
from typing import Any


@dataclass
class LiveDevice:
    device_id: int
    key: str                # stable slug used to build this device's NodeIds
    folder_node: Any        # ua.Node - the device's folder/object node
    uuid_node: Any          # ua.Node - the DeviceUUID child variable


@dataclass
class LiveTag:
    tag_id: int
    device_id: int
    node: Any                # ua.Node - the value variable
    name: str
    data_type: str            # "Boolean" | "Double" | "Int32" | "String"
    unit: str = ""
    behavior: Any = None      # live lib.behaviors.Behavior instance

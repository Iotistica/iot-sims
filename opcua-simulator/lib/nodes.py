"""
OPC UA node creation and live CRUD — Device (folder) -> Tag (variable)
hierarchy, driven by the SQLite-backed devices/tags tables (lib/db.py)
instead of static profile JSON.

Live add/delete after the server has started is confirmed safe (verified
against an already-connected, subscribed client via a throwaway spike before
this module was written) — asyncua's Node.delete() and add_variable() both
work correctly post-startup.
"""
import logging
from typing import Dict, Optional

from asyncua import Server, ua

from .types import LiveDevice, LiveTag

logger = logging.getLogger(__name__)

NS_URI = "http://iotistica.com/opcua-simulator"

_VARIANT_TYPES = {
    "Boolean": ua.VariantType.Boolean,
    "Double": ua.VariantType.Double,
    "Int32": ua.VariantType.Int32,
    "String": ua.VariantType.String,
}

_DEFAULT_VALUES = {
    "Boolean": False,
    "Double": 0.0,
    "Int32": 0,
    "String": "",
}


class NodeManager:
    """Creates/deletes OPC UA nodes for devices and tags, keyed by DB id."""

    def __init__(self, server: Server):
        self.server = server
        self.idx = 2  # placeholder until register_namespace() runs
        self._devices: Dict[int, LiveDevice] = {}
        self._tags: Dict[int, LiveTag] = {}

    async def register_namespace(self) -> int:
        self.idx = await self.server.register_namespace(NS_URI)
        return self.idx

    # ── Devices ──────────────────────────────────────────────────────────────

    async def create_device(self, device_row: dict) -> LiveDevice:
        key = device_row["key"]
        objects = self.server.nodes.objects
        folder = await objects.add_folder(ua.NodeId(key, self.idx), device_row["name"])
        uuid_var = await folder.add_variable(
            ua.NodeId(f"{key}/DeviceUUID", self.idx), "DeviceUUID", key
        )
        await uuid_var.set_writable(False)
        live = LiveDevice(device_id=device_row["id"], key=key, folder_node=folder, uuid_node=uuid_var)
        self._devices[device_row["id"]] = live
        return live

    async def delete_device(self, device_id: int) -> None:
        live = self._devices.pop(device_id, None)
        if not live:
            return
        # Recursive delete removes DeviceUUID + every tag variable under this folder in one call.
        await live.folder_node.delete(delete_references=True, recursive=True)
        for tag_id in [tid for tid, t in self._tags.items() if t.device_id == device_id]:
            self._tags.pop(tag_id, None)

    def get_device(self, device_id: int) -> Optional[LiveDevice]:
        return self._devices.get(device_id)

    def get_all_devices(self) -> list[LiveDevice]:
        return list(self._devices.values())

    # ── Tags ─────────────────────────────────────────────────────────────────

    async def create_tag(self, device_id: int, tag_row: dict, behavior) -> LiveTag:
        live_device = self._devices.get(device_id)
        if not live_device:
            raise ValueError(f"Device {device_id} has no live node — create the device first")

        data_type = tag_row["data_type"]
        node_id = ua.NodeId(f"{live_device.key}/{tag_row['name']}", self.idx)
        var = await live_device.folder_node.add_variable(
            node_id, tag_row["name"], _DEFAULT_VALUES[data_type],
            varianttype=_VARIANT_TYPES[data_type],
        )
        if tag_row.get("writable"):
            await var.set_writable()
        if tag_row.get("unit"):
            try:
                await var.write_attribute(
                    ua.AttributeIds.Description, ua.DataValue(ua.LocalizedText(tag_row["unit"]))
                )
            except Exception as e:
                logger.debug("Could not set unit description for %s: %s", tag_row["name"], e)

        live_tag = LiveTag(
            tag_id=tag_row["id"], device_id=device_id, node=var, name=tag_row["name"],
            data_type=data_type, unit=tag_row.get("unit", ""), behavior=behavior,
        )
        self._tags[tag_row["id"]] = live_tag
        return live_tag

    async def delete_tag(self, tag_id: int) -> None:
        live = self._tags.pop(tag_id, None)
        if not live:
            return
        await live.node.delete(delete_references=True)

    def get_tag(self, tag_id: int) -> Optional[LiveTag]:
        return self._tags.get(tag_id)

    def get_tags_for_device(self, device_id: int) -> list[LiveTag]:
        return [t for t in self._tags.values() if t.device_id == device_id]

    def get_all_tags(self) -> list[LiveTag]:
        return list(self._tags.values())

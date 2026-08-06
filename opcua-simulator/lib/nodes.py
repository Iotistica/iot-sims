"""
OPC UA node creation and live CRUD — Folder -> Device (folder) -> Tag
(variable) hierarchy, driven by the SQLite-backed folders/devices/tags
tables (lib/db.py) instead of static profile JSON.

Live add/delete after the server has started is confirmed safe (verified
against an already-connected, subscribed client via a throwaway spike before
this module was written) — asyncua's Node.delete() and add_variable() both
work correctly post-startup.

NodeIds are stable and id-based (`folder/<id>`, `device/<id>`, `tag/<id>`),
not derived from name/key/parent path — a folder or device can be renamed or
moved to a different parent without changing its own or any descendant's
NodeId. See docs/address-space-modeling.md for the full rationale and for
why Device is still created as FolderType here (a separate, deliberately
deferred correction, not an oversight of this module).

Reference-type selection (Organizes vs HasComponent) is NOT hand-rolled
here — asyncua's own add_folder()/add_object()/add_variable() helpers
already choose the spec-correct reference automatically based on the
resolved parent node's actual TypeDefinition at creation time (confirmed by
reading asyncua/common/manage_nodes.py directly). This module only needs to
resolve the right parent Node handle and call those helpers.
"""
import logging
from typing import Dict, Optional

from asyncua import Server, ua

from .types import LiveDevice, LiveFolder, LiveTag

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
    """Creates/deletes OPC UA nodes for folders, devices, and tags, keyed by DB id."""

    def __init__(self, server: Server):
        self.server = server
        self.idx = 2  # placeholder until register_namespace() runs
        self._folders: Dict[int, LiveFolder] = {}
        self._devices: Dict[int, LiveDevice] = {}
        self._tags: Dict[int, LiveTag] = {}

    async def register_namespace(self) -> int:
        self.idx = await self.server.register_namespace(NS_URI)
        return self.idx

    # ── Folders ──────────────────────────────────────────────────────────────

    async def create_folder(self, folder_row: dict, parent_node=None) -> LiveFolder:
        """parent_node: another live folder's `.node`, or None for root
        (directly under Objects) — caller resolves this by walking
        folder_row['parent_folder_id'] against self._folders, since only the
        caller (SimEngine) knows the creation order needed to have already
        created that parent."""
        parent = parent_node if parent_node is not None else self.server.nodes.objects
        node = await parent.add_folder(ua.NodeId(f"folder/{folder_row['id']}", self.idx), folder_row["name"])
        live = LiveFolder(folder_id=folder_row["id"], node=node)
        self._folders[folder_row["id"]] = live
        return live

    async def delete_folder(self, folder_id: int) -> None:
        live = self._folders.pop(folder_id, None)
        if not live:
            return
        # Recursive delete — callers are responsible for having already torn
        # down (or never created) any devices/sub-folders still registered
        # under this one; this only removes what's actually live on the node
        # itself and below it in the address space.
        await live.node.delete(delete_references=True, recursive=True)

    def get_folder(self, folder_id: int) -> Optional[LiveFolder]:
        return self._folders.get(folder_id)

    def get_all_folders(self) -> list[LiveFolder]:
        return list(self._folders.values())

    # ── Devices ──────────────────────────────────────────────────────────────

    async def create_device(self, device_row: dict, parent_node=None) -> LiveDevice:
        """parent_node: a live folder's `.node`, or None for root (directly
        under Objects) — today's exact behavior when the device has no
        folder_id."""
        parent = parent_node if parent_node is not None else self.server.nodes.objects
        device_id = device_row["id"]
        folder = await parent.add_folder(ua.NodeId(f"device/{device_id}", self.idx), device_row["name"])
        uuid_var = await folder.add_variable(
            ua.NodeId(f"device/{device_id}/uuid", self.idx), "DeviceUUID", device_row["key"]
        )
        await uuid_var.set_writable(False)
        # EventNotifier is only a valid attribute on Object/View nodes (not
        # Variables, per spec) — set here on the device's own Object node so
        # it can be used as an Event/Condition source (see lib/alarms.py).
        #
        # NOTE: asyncua's event delivery (monitored_item_service.trigger_event)
        # matches only the *exact* subscribed source node — it does not walk
        # HasNotifier references hierarchically, so subscribing to the Server
        # node (the usual "give me everything" pattern) will NOT receive
        # events sourced from this device; a client must subscribe directly
        # to this device's node. ConditionRefresh (asyncua has this built in
        # already — see subscription_service.py's condition_refresh) is the
        # one path that isn't subject to this: it replays all currently
        # Retain=True conditions server-wide regardless of which node a
        # client's monitored item is on.
        await folder.set_event_notifier([ua.EventNotifier.SubscribeToEvents])
        live = LiveDevice(device_id=device_id, key=device_row["key"], folder_node=folder, uuid_node=uuid_var)
        self._devices[device_id] = live
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
        node_id = ua.NodeId(f"tag/{tag_row['id']}", self.idx)
        var = await live_device.folder_node.add_variable(
            node_id, tag_row["name"], _DEFAULT_VALUES[data_type],
            varianttype=_VARIANT_TYPES[data_type],
        )
        if tag_row.get("writable"):
            await var.set_writable()
        if tag_row.get("unit"):
            unit_text = tag_row["unit"]
            try:
                await var.write_attribute(
                    ua.AttributeIds.Description, ua.DataValue(ua.LocalizedText(unit_text))
                )
            except Exception as e:
                logger.debug("Could not set unit description for %s: %s", tag_row["name"], e)
            # Standard OPC-UA unit exposure (Part 8, EUInformation) — a child
            # "EngineeringUnits" property, same as a real AnalogItemType server
            # would expose. The Description-attribute write above is kept as a
            # secondary/legacy signal for simpler clients that don't browse for
            # the real property.
            try:
                eu_info = ua.EUInformation()
                eu_info.DisplayName = ua.LocalizedText(unit_text)
                eu_info.Description = ua.LocalizedText(unit_text)
                await var.add_property(
                    ua.NodeId(f"tag/{tag_row['id']}/EngineeringUnits", self.idx),
                    "EngineeringUnits",
                    eu_info,
                    varianttype=ua.VariantType.ExtensionObject,
                )
            except Exception as e:
                logger.debug("Could not add EngineeringUnits property for %s: %s", tag_row["name"], e)

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
        # recursive=True: tags with a unit now own a child EngineeringUnits
        # property node (see create_tag) that would otherwise be orphaned.
        await live.node.delete(delete_references=True, recursive=True)

    def get_tag(self, tag_id: int) -> Optional[LiveTag]:
        return self._tags.get(tag_id)

    def get_tags_for_device(self, device_id: int) -> list[LiveTag]:
        return [t for t in self._tags.values() if t.device_id == device_id]

    def get_all_tags(self) -> list[LiveTag]:
        return list(self._tags.values())

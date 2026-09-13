"""Device construction boundary -- the physical move `SimEngine._make_device_object()`'s own
comment used to promise. Builds one BACnet `DeviceObject` from a raw device row dict; called by
`BacnetProtocolAdapter` (src/simulation/protocols/bacnet.py), both from its own `start()` batch
and, indirectly via `SimEngine.add_object_hot()`, from a hot-add.

Takes `engine` (the owning `SimEngine`) even though this particular function reads nothing off
it today -- kept for signature symmetry with `object_factory.create_object()`, which genuinely
does need engine state (`_allocate_object_identifier`, `_point_behaviors`,
`_reload_preserved_values`), and so a caller holding just `engine` can reach either factory the
same way.
"""
from __future__ import annotations

from typing import Any

from bacpypes3.basetypes import Segmentation
from bacpypes3.local.device import DeviceObject
from bacpypes3.primitivedata import Unsigned


def make_device_object(engine: Any, dev: dict) -> DeviceObject:
    try:
        segmentation = Segmentation(dev.get("segmentation_supported") or "segmented-both")
    except Exception:
        segmentation = Segmentation("segmented-both")
    return DeviceObject(
        objectIdentifier=f"device,{dev['device_instance']}",
        objectName=dev["name"],
        vendorIdentifier=999,
        description=dev.get("description", ""),
        modelName=dev.get("model_name", "BACnet Simulator"),
        vendorName=dev.get("vendor_name", "Iotistica"),
        applicationSoftwareVersion="3.0",
        location=dev["name"],
        firmwareRevision=dev.get("firmware_revision") or "N/A",
        protocolRevision=Unsigned(dev.get("protocol_revision") or 22),
        maxApduLengthAccepted=Unsigned(dev.get("max_apdu_length_accepted") or 1024),
        segmentationSupported=segmentation,
    )

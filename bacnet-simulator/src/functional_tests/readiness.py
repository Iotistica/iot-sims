"""Pre-flight readiness for Functional Test execution.

Replaces the old semantic point_type -> object resolution (resolution.py,
deleted): now that every point reference in a saved definition is already a
concrete PointRef ({device_id, object_id}) chosen at authoring time, there's
nothing left to resolve at Run time -- only to verify. "Readiness" is
strictly existence + a simulation-only check for Set nodes, never
ambiguity resolution.

Two-step shape, mirroring the old module's separation: collect_point_refs()
walks the definition for every point reference (deduplicated, tagged with
whether it's a Set node's write target); build_point_cache() turns that into
the {(device_id, object_id): {"object":, "device":}} map TestRuntime is
constructed from; check_readiness() is the read-only preview both /resolve
and /runs' pre-flight check use.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Optional


def _walk_point_refs(definition: dict) -> Iterator[tuple[dict, bool]]:
    """Yields (point_ref, is_set_target) for every concrete PointRef
    referenced anywhere in the definition -- wait_until's point and (when
    its value operand is point-kind) that operand's point, capture's point,
    set's point (tagged is_set_target=True), and verify/compare's left/right
    operands when point-kind."""
    for node in definition.get("nodes", []):
        node_type = node.get("type")
        params = node.get("params") or {}

        if node_type in ("wait_until", "capture", "set"):
            point = params.get("point")
            if isinstance(point, dict):
                yield point, node_type == "set"
            if node_type == "wait_until":
                value = params.get("value")
                if isinstance(value, dict) and value.get("kind") == "point":
                    operand_point = value.get("point")
                    if isinstance(operand_point, dict):
                        yield operand_point, False

        elif node_type in ("verify", "compare"):
            for side in ("left", "right"):
                operand = params.get(side) or {}
                if operand.get("kind") == "point":
                    operand_point = operand.get("point")
                    if isinstance(operand_point, dict):
                        yield operand_point, False


def collect_point_refs(definition: dict) -> list[dict]:
    """Deduplicated by (device_id, object_id); `used_by_set` is True if any
    node referencing that point is a Set node's write target (a point can in
    principle be both read elsewhere and written by Set -- that still counts
    as used_by_set for the simulation-only readiness check)."""
    seen: dict[tuple[Any, Any], dict] = {}
    for point, is_set_target in _walk_point_refs(definition):
        key = (point.get("device_id"), point.get("object_id"))
        entry = seen.setdefault(
            key,
            {"device_id": point.get("device_id"), "object_id": point.get("object_id"), "used_by_set": False},
        )
        if is_set_target:
            entry["used_by_set"] = True
    return list(seen.values())


def build_point_cache(database: Any, point_refs: list[dict]) -> dict[tuple[int, int], dict]:
    """One get_device/get_object call per distinct id, not per point
    reference -- cheap even for a large definition."""
    cache: dict[tuple[int, int], dict] = {}
    devices: dict[int, Optional[dict]] = {}
    for ref in point_refs:
        device_id = ref["device_id"]
        object_id = ref["object_id"]
        if device_id not in devices:
            devices[device_id] = database.get_device(device_id)
        device = devices[device_id]
        if device is None:
            continue
        obj = database.get_object(object_id)
        if obj is None or obj.get("device_id") != device_id:
            continue
        cache[(device_id, object_id)] = {"object": obj, "device": device}
    return cache


@dataclass
class PointReadiness:
    device_id: Optional[int]
    object_id: Optional[int]
    status: str  # "ok" | "missing_device" | "missing_object" | "not_simulated"
    device_name: Optional[str] = None
    object_name: Optional[str] = None
    message: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "device_id": self.device_id,
            "object_id": self.object_id,
            "status": self.status,
            "device_name": self.device_name,
            "object_name": self.object_name,
            "message": self.message,
        }


def check_readiness(database: Any, definition: dict) -> list[PointReadiness]:
    results: list[PointReadiness] = []
    for ref in collect_point_refs(definition):
        device_id = ref["device_id"]
        object_id = ref["object_id"]

        device = database.get_device(device_id) if device_id is not None else None
        if device is None:
            results.append(PointReadiness(
                device_id=device_id, object_id=object_id, status="missing_device",
                message=f"Device {device_id!r} no longer exists",
            ))
            continue

        obj = database.get_object(object_id) if object_id is not None else None
        if obj is None or obj.get("device_id") != device_id:
            results.append(PointReadiness(
                device_id=device_id, object_id=object_id, status="missing_object",
                device_name=device.get("name"),
                message=f"Point no longer exists on {device.get('name')}",
            ))
            continue

        if ref["used_by_set"] and device.get("source_type") == "external-bacnet":
            results.append(PointReadiness(
                device_id=device_id, object_id=object_id, status="not_simulated",
                device_name=device.get("name"), object_name=obj.get("name"),
                message=(
                    f"{device.get('name')} / {obj.get('name')} is an external device -- "
                    "Set is only supported against simulated devices"
                ),
            ))
            continue

        results.append(PointReadiness(
            device_id=device_id, object_id=object_id, status="ok",
            device_name=device.get("name"), object_name=obj.get("name"),
        ))
    return results

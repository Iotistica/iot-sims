"""The protocol-adapter foundation: `ProtocolAdapter` is the seam between SimEngine (how
a value is computed, when ticks occur) and a wire protocol (how a device/point is exposed and how
a computed value reaches it) -- see src/simulation/protocols/base.py's own module docstring.

Four things are tested here, at the levels the plan calls for:
  1. `SimEngine._protocol_of()` -- a pure static helper, unit-tested directly against artificial
     dicts. There is deliberately no implicit `protocol="bacnet"` fallback (see the "no implicit
     protocol" rule this foundation is built on) -- a row with no explicit protocol is a bug, not
     something to paper over, so this raises rather than guessing. A real migrated database row
     never lacks one (migration 32 backfills `protocol='bacnet'` for every row -- see
     test_db_migrations.py's own migration-32 test for that guarantee), and `Database.create_device`/
     `update_device` both require an explicit `protocol` on every write going forward.
  2. `SimEngine._device_runtime_config()`/`_point_runtime_config()` -- the translation from a raw
     DB row dict into the protocol-neutral dataclasses every ProtocolAdapter is handed.
  3. `BacnetProtocolAdapter`'s mechanical delegate methods, exercised directly -- mirrors
     test_fault_restore.py's own established `object_factory.create_object(...)`-direct-call
     convention for building a real bacpypes3 object without running a full engine start().
     SimEngine no longer constructs `BacnetProtocolAdapter` itself (that's the composition
     layer's job -- see src/simulation/runtime.py's `lifespan()`), so every test here that needs
     one registers it explicitly via `register_protocol_adapter()`, the same as any other
     protocol would.
  4. The full real lifecycle acceptance test: a `RecordingProtocolAdapter` (a minimal non-BACnet
     adapter -- no socket, no wire protocol, just an in-memory call log) proves a normal simulated
     point can be computed by the existing engine and delivered through it during a real
     `start()` -> `tick()` -> `stop()` cycle, without constructing any BACnet runtime state at
     all -- the Step 1 acceptance criterion, verbatim.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.bacnet import object_factory
from src.simulation.engine import SimEngine
from src.simulation.protocol_runtime import (
    DeviceRuntimeConfig, PointQuality, PointRuntimeConfig, PointValue,
)
from src.simulation.protocols.bacnet import BacnetProtocolAdapter
from src.simulation.protocols.base import ProtocolAdapter, ProtocolContext


# ═══════════════════════════════════════════════════════════════════════════
# 1. _protocol_of() -- no implicit protocol, ever
# ═══════════════════════════════════════════════════════════════════════════

def test_protocol_of_raises_for_missing_or_none_protocol():
    """No implicit protocol="bacnet": a row without an explicit protocol is rejected outright,
    never silently coerced. The only place this could ever fire is a hand-built dict (this test)
    or defensive code that bypasses the DB -- see the module docstring."""
    with pytest.raises(ValueError):
        SimEngine._protocol_of({})
    with pytest.raises(ValueError):
        SimEngine._protocol_of({"protocol": None})
    assert SimEngine._protocol_of({"protocol": "opcua"}) == "opcua"


# ═══════════════════════════════════════════════════════════════════════════
# 2. Database row -> DeviceRuntimeConfig/PointRuntimeConfig translation
# ═══════════════════════════════════════════════════════════════════════════

def test_device_runtime_config_carries_the_full_raw_row_as_protocol_config():
    device_row = {
        "id": 1, "name": "RTU-1", "device_instance": 101, "protocol": "bacnet",
        "description": "Rooftop unit", "vendor_name": "Iotistica",
    }
    config = SimEngine._device_runtime_config(device_row)
    assert isinstance(config, DeviceRuntimeConfig)
    assert config.device_id == 1
    assert config.name == "RTU-1"
    assert config.protocol == "bacnet"
    # protocol_config carries the WHOLE raw row -- not just device_instance -- so a Step 1
    # delegate method calling back into a raw-dict-expecting SimEngine helper needs no second
    # DB round-trip.
    assert config.protocol_config["description"] == "Rooftop unit"
    assert config.protocol_config["vendor_name"] == "Iotistica"


def test_device_runtime_config_raises_for_missing_protocol():
    with pytest.raises(ValueError):
        SimEngine._device_runtime_config({"id": 2, "name": "Legacy-Device"})


def test_point_runtime_config_carries_the_full_raw_row_as_protocol_config():
    obj_row = {
        "id": 55, "device_id": 1, "name": "Zone-Temp", "units": "degrees-celsius",
        "object_type": "analog-input", "behavior": "constant",
    }
    config = SimEngine._point_runtime_config(obj_row)
    assert isinstance(config, PointRuntimeConfig)
    assert config.point_id == 55
    assert config.device_id == 1
    assert config.name == "Zone-Temp"
    assert config.units == "degrees-celsius"
    assert config.protocol_config["object_type"] == "analog-input"


# ═══════════════════════════════════════════════════════════════════════════
# 3. BacnetProtocolAdapter -- mechanical delegate methods, exercised directly
# ═══════════════════════════════════════════════════════════════════════════

def _bacnet_adapter(engine: SimEngine) -> BacnetProtocolAdapter:
    """SimEngine no longer constructs BacnetProtocolAdapter itself -- production wiring does
    this once in src/simulation/runtime.py's lifespan(); every test below does the same explicit
    registration a real composition root would."""
    adapter = BacnetProtocolAdapter(engine)
    engine.register_protocol_adapter("bacnet", adapter)
    return adapter


def test_bacnet_adapter_add_device_builds_a_device_object(database):
    engine = SimEngine(database)
    adapter = _bacnet_adapter(engine)
    device_config = DeviceRuntimeConfig(
        device_id=1, name="Bacnet-Device", protocol="bacnet",
        protocol_config={
            "device_instance": 9802, "name": "Bacnet-Device", "description": "",
            "segmentation_supported": "segmented-both",
        },
    )
    dev_obj = adapter.add_device(device_config)
    identifier = dev_obj.objectIdentifier
    assert str(identifier[0]) == "device"
    assert int(identifier[1]) == 9802
    assert str(dev_obj.objectName) == "Bacnet-Device"


def test_bacnet_adapter_add_point_creates_a_live_bacnet_object(database):
    engine = SimEngine(database)
    adapter = _bacnet_adapter(engine)
    device_config = DeviceRuntimeConfig(
        device_id=1, name="Bacnet-Device-2", protocol="bacnet",
        protocol_config={
            "device_instance": 9803, "name": "Bacnet-Device-2", "description": "",
            "segmentation_supported": "segmented-both",
        },
    )
    dev_obj = adapter.add_device(device_config)
    # add_point()'s slot lookup keys off SimEngine._device_slots -- same convention
    # add_object_hot() already uses for a hot-added point on an already-running device.
    engine._device_slots[9803] = 0

    point_config = PointRuntimeConfig(
        point_id=90001, device_id=1, name="Zone-Temp", units="degrees-celsius",
        protocol_config={
            "id": 90001, "object_type": "analog-input", "object_instance": 1,
            "name": "Zone-Temp", "units": "degrees-celsius", "behavior": "constant",
            "behavior_params": json.dumps({"value": 21.5}),
            "manual_value": None, "reliability": "no-fault-detected",
        },
    )
    point_handle = adapter.add_point(dev_obj, point_config)
    assert str(point_handle.objectIdentifier[0]) == "analog-input"
    assert float(point_handle.presentValue) == 21.5


def test_bacnet_adapter_write_point_writes_value_and_maps_offline_quality_to_reliability(database):
    """Confirms the attribute-stashing design (_create_object() stashes _sim_object_type/
    _sim_configured_reliability onto the live object at creation time) actually lets
    write_point() -- which is handed only (handle, PointValue), no obj_row -- reproduce the exact
    same Reliability/Status_Flags behavior _offline_reliability_override() defines."""
    engine = SimEngine(database)
    adapter = _bacnet_adapter(engine)
    obj_row = {
        "id": 90002, "object_type": "analog-input", "object_instance": 2,
        "name": "Zone-Temp-2", "units": "degrees-celsius",
        "behavior": "constant", "behavior_params": json.dumps({"value": 10}),
        "manual_value": None, "reliability": "no-fault-detected",
    }
    bacnet_obj, _behavior = object_factory.create_object(engine, obj_row, slot=0, device_name="RTU")

    written = adapter.write_point(bacnet_obj, PointValue(value=55.0, quality=PointQuality.GOOD))
    assert written == 55.0
    assert float(bacnet_obj.presentValue) == 55.0
    assert str(bacnet_obj.reliability) == "no-fault-detected"
    assert bool(bacnet_obj.statusFlags[1]) is False

    adapter.write_point(bacnet_obj, PointValue(value=55.0, quality=PointQuality.OFFLINE))
    assert str(bacnet_obj.reliability) == "communication-failure"
    assert bool(bacnet_obj.statusFlags[1]) is True

    # Recovery self-corrects with no dedicated cleanup, same as _offline_reliability_override().
    adapter.write_point(bacnet_obj, PointValue(value=55.0, quality=PointQuality.GOOD))
    assert str(bacnet_obj.reliability) == "no-fault-detected"
    assert bool(bacnet_obj.statusFlags[1]) is False


def test_bacnet_adapter_remove_device_and_remove_point_are_no_ops_without_a_running_app(database):
    """BacnetProtocolAdapter.start()'s own batch (and stop()'s own batch teardown) never call
    these individually -- BACnet stays one coupled batch, not decomposed into per-device/per-point
    calls like every other protocol (see BacnetProtocolAdapter's own module docstring); these
    exist for interface completeness. Confirms they degrade safely (no exception) when self.app is
    None, exactly the state a null-only project's engine is in."""
    engine = SimEngine(database)
    adapter = _bacnet_adapter(engine)
    assert engine.app is None
    adapter.remove_device("not-a-real-handle")
    adapter.remove_point("not-a-real-handle-either")


# ═══════════════════════════════════════════════════════════════════════════
# 4. Full lifecycle acceptance test -- a non-BACnet ProtocolAdapter, start() -> tick() -> stop()
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class _RecordedWrite:
    handle: Any
    update: PointValue


@dataclass
class RecordingProtocolAdapter(ProtocolAdapter):
    """A minimal non-BACnet ProtocolAdapter -- no socket, no wire protocol, just an in-memory log
    of every call SimEngine makes. The whole point of the acceptance test below is proving the
    engine's real start()->tick()->stop() lifecycle can drive value computation and delivery
    through something that ISN'T BACnet, without ever touching bacpypes3/BACnet runtime state."""
    protocol: str = "null"
    start_calls: list[ProtocolContext] = field(default_factory=list)
    stop_calls: int = 0
    added_devices: list[DeviceRuntimeConfig] = field(default_factory=list)
    removed_devices: list[Any] = field(default_factory=list)
    added_points: list[tuple[Any, PointRuntimeConfig]] = field(default_factory=list)
    removed_points: list[Any] = field(default_factory=list)
    writes: list[_RecordedWrite] = field(default_factory=list)
    _next_handle: int = 0

    async def start(self, context: ProtocolContext) -> None:
        self.start_calls.append(context)

    async def stop(self) -> None:
        self.stop_calls += 1

    def add_device(self, device: DeviceRuntimeConfig) -> Any:
        self.added_devices.append(device)
        self._next_handle += 1
        return f"device-handle-{self._next_handle}"

    def remove_device(self, handle: Any) -> None:
        self.removed_devices.append(handle)

    def add_point(self, device_handle: Any, point: PointRuntimeConfig) -> Any:
        self._next_handle += 1
        handle = f"point-handle-{self._next_handle}"
        self.added_points.append((device_handle, point))
        return handle

    def remove_point(self, handle: Any) -> None:
        self.removed_points.append(handle)

    def write_point(self, handle: Any, update: PointValue) -> Any:
        self.writes.append(_RecordedWrite(handle=handle, update=update))
        return update.value


async def test_null_protocol_point_completes_full_lifecycle_without_any_bacnet_runtime(database):
    # protocol is a first-class, required field on device creation -- no API/UI surface offers
    # anything but "bacnet" yet, but the DB/engine layer itself makes no such assumption.
    device = database.create_device({
        "protocol": "null", "device_instance": 9801, "name": "Null-Device", "description": "",
        "vendor_name": "Iotistica", "model_name": "Null", "enabled": 1,
        "firmware_revision": "N/A", "protocol_revision": 22, "max_apdu_length_accepted": 1024,
        "segmentation_supported": "segmented-both",
    })

    created_obj = database.create_object(device["id"], {
        "object_type": "analog-input", "object_instance": 1, "name": "Zone-Temp",
        "units": "degrees-celsius", "behavior": "constant",
        "behavior_params": json.dumps({"value": 42.0}),
        "enabled": 1, "number_of_states": 2, "reliability": "no-fault-detected",
        "polarity": "normal", "point_type": None,
    })

    engine = SimEngine(database)
    recorder = RecordingProtocolAdapter()
    engine._protocol_adapters["null"] = recorder

    await engine.start()

    # -- start() called; device/point registered --
    assert len(recorder.start_calls) == 1
    assert [d.device_id for d in recorder.start_calls[0].devices] == [device["id"]]
    assert len(recorder.added_devices) == 1
    assert recorder.added_devices[0].device_id == device["id"]
    assert len(recorder.added_points) == 1
    added_device_handle, added_point_config = recorder.added_points[0]
    assert added_device_handle == "device-handle-1"
    assert added_point_config.point_id == created_obj["id"]

    # -- no BACnet runtime touched at all --
    assert engine.app is None
    assert engine.network_port is None
    assert "bacnet" not in engine._active_adapters
    assert "null" in engine._active_adapters

    # -- the point is tracked generically, through the non-BACnet adapter --
    # (handle numbering: add_device() claims handle 1, add_point() claims handle 2 -- both
    # count off the same recorder-wide counter.)
    runtime_point = engine._runtime_points[created_obj["id"]]
    assert runtime_point.adapter is recorder
    assert runtime_point.handle == "point-handle-2"

    await engine.tick()

    # -- provider/Behavior computation occurred, and the computed value reached write_point() --
    assert len(recorder.writes) == 1
    assert recorder.writes[0].handle == runtime_point.handle
    assert recorder.writes[0].update.value == pytest.approx(42.0)
    assert recorder.writes[0].update.quality == PointQuality.GOOD

    await engine.stop()

    # -- adapter.stop() called, non-BACnet teardown ran, neutral registry emptied --
    assert recorder.stop_calls == 1
    assert recorder.removed_points == [runtime_point.handle]
    assert recorder.removed_devices == ["device-handle-1"]
    assert engine._runtime_points == {}
    assert engine._active_adapters == set()
    assert engine.app is None

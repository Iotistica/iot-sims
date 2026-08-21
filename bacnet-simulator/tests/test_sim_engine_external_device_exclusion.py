"""SimEngine._simulated_enabled_devices() is the single filter every live
BACnet-visible effect (virtual-device registration, object creation, the
I-Am announcement loop, Who-Is/I-Am handling) cascades from -- see
SimEngine.start(). Tested as a pure function, no socket bind needed (this
repo's test suite deliberately never calls engine.start()/reload()
directly -- see conftest.py's docstring)."""
from __future__ import annotations

from src.simulation.engine import SimEngine


def test_external_device_excluded_even_when_enabled():
    devices = [
        {"enabled": 1, "source_type": "simulated", "device_instance": 1},
        {"enabled": 1, "source_type": "external-bacnet", "device_instance": 2},
    ]
    result = SimEngine._simulated_enabled_devices(devices)
    assert [d["device_instance"] for d in result] == [1]


def test_disabled_simulated_device_still_excluded():
    devices = [
        {"enabled": 0, "source_type": "simulated", "device_instance": 1},
        {"enabled": 1, "source_type": "simulated", "device_instance": 2},
    ]
    result = SimEngine._simulated_enabled_devices(devices)
    assert [d["device_instance"] for d in result] == [2]


def test_missing_source_type_key_defaults_to_simulated():
    """Pre-migration device dicts (or any caller not yet passing
    source_type) must default to included -- backward compatibility."""
    devices = [{"enabled": 1, "device_instance": 1}]
    result = SimEngine._simulated_enabled_devices(devices)
    assert [d["device_instance"] for d in result] == [1]


def test_enabled_external_device_never_included_regardless_of_order():
    devices = [
        {"enabled": 1, "source_type": "external-bacnet", "device_instance": 10},
        {"enabled": 1, "source_type": "external-bacnet", "device_instance": 11},
        {"enabled": 1, "source_type": "simulated", "device_instance": 12},
    ]
    result = SimEngine._simulated_enabled_devices(devices)
    assert [d["device_instance"] for d in result] == [12]


def test_empty_device_list():
    assert SimEngine._simulated_enabled_devices([]) == []


def test_all_external_yields_empty_list():
    devices = [
        {"enabled": 1, "source_type": "external-bacnet", "device_instance": 1},
        {"enabled": 1, "source_type": "external-bacnet", "device_instance": 2},
    ]
    # An empty result here is exactly what makes SimEngine.start() take its
    # "no enabled devices -- BACnet stack idle" early-return path (self.app
    # stays None), which is the whole point: a project containing only
    # external devices must never bind a BACnet socket announcing anything.
    assert SimEngine._simulated_enabled_devices(devices) == []

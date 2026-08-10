"""Database.sync_external_devices() reconciliation: update-in-place on
rediscovery, never delete a device just because one run didn't see it."""
from __future__ import annotations


def _by_instance(devices):
    return {d["device_instance"]: d for d in devices}


def test_sync_creates_new_external_devices(database):
    persisted = database.sync_external_devices([
        {
            "device_instance": 1000, "name": "bms_gateway_1000", "host": "172.22.0.21", "port": 47808,
            "metadata": {"objectName": "BMS-Gateway", "vendorName": "Honeywell", "vendorId": 5},
        },
    ])
    assert len(persisted) == 1
    dev = persisted[0]
    assert dev["source_type"] == "external-bacnet"
    assert dev["name"] == "BMS-Gateway"
    assert dev["vendor_name"] == "Honeywell"
    assert dev["external_host"] == "172.22.0.21"
    assert dev["external_vendor_id"] == 5


def test_sync_updates_existing_device_in_place_not_duplicated(database):
    first = database.sync_external_devices([
        {"device_instance": 1000, "name": "d", "host": "172.22.0.21", "port": 47808,
         "metadata": {"objectName": "Original-Name", "vendorName": "Honeywell"}},
    ])
    second = database.sync_external_devices([
        {"device_instance": 1000, "name": "d", "host": "172.22.0.21", "port": 47808,
         "metadata": {"objectName": "Renamed", "vendorName": "Honeywell"}},
    ])
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["name"] == "Renamed"


def test_rediscover_missing_device_is_not_deleted(database):
    database.sync_external_devices([
        {"device_instance": 1000, "name": "d1", "host": "172.22.0.21", "port": 47808, "metadata": {}},
        {"device_instance": 1001, "name": "d2", "host": "172.22.0.21", "port": 47808, "metadata": {}},
    ])
    # Second run only sees 1000 -- 1001 is "temporarily offline".
    persisted = database.sync_external_devices([
        {"device_instance": 1000, "name": "d1", "host": "172.22.0.21", "port": 47808, "metadata": {}},
    ])
    instances = {d["device_instance"] for d in persisted}
    assert instances == {1000, 1001}


def test_rediscover_adds_newly_seen_device_without_touching_others(database):
    database.sync_external_devices([
        {"device_instance": 1000, "name": "d1", "host": "172.22.0.21", "port": 47808, "metadata": {}},
    ])
    persisted = database.sync_external_devices([
        {"device_instance": 1000, "name": "d1", "host": "172.22.0.21", "port": 47808, "metadata": {}},
        {"device_instance": 1002, "name": "d3", "host": "172.22.0.21", "port": 47808, "metadata": {}},
    ])
    instances = {d["device_instance"] for d in persisted}
    assert instances == {1000, 1002}


def test_sync_external_objects_upserts_by_type_and_instance(database):
    devices = database.sync_external_devices([
        {"device_instance": 1000, "name": "d1", "host": "172.22.0.21", "port": 47808, "metadata": {}},
    ])
    device_id = devices[0]["id"]

    first = database.sync_external_objects(device_id, [
        {"object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius", "description": None},
    ])
    assert len(first) == 1

    second = database.sync_external_objects(device_id, [
        {"object_type": "analog-input", "object_instance": 1, "name": "SAT-renamed", "units": "degrees-celsius", "description": "supply air temp"},
    ])
    assert len(second) == 1
    assert second[0]["id"] == first[0]["id"]
    assert second[0]["name"] == "SAT-renamed"
    assert second[0]["description"] == "supply air temp"


def test_sync_external_objects_never_touches_manual_value(database):
    devices = database.sync_external_devices([
        {"device_instance": 1000, "name": "d1", "host": "172.22.0.21", "port": 47808, "metadata": {}},
    ])
    device_id = devices[0]["id"]
    rows = database.sync_external_objects(device_id, [
        {"object_type": "analog-input", "object_instance": 1, "name": "SAT", "units": "degrees-celsius"},
    ])
    assert rows[0]["manual_value"] is None
    assert rows[0]["behavior"] == "constant"  # untouched simulation-only default


def test_touch_external_device_last_seen(database):
    devices = database.sync_external_devices([
        {"device_instance": 1000, "name": "d1", "host": "172.22.0.21", "port": 47808, "metadata": {}},
    ])
    assert devices[0]["external_last_seen_at"] is not None
    device_id = devices[0]["id"]
    database.touch_external_device_last_seen(device_id)
    updated = database.get_device(device_id)
    assert updated["external_last_seen_at"] is not None

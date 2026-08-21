"""API-level tests for GET /packet-capture/packets?device_id=/&unassociated=
and the list/detail consistency of the new simulator_* fields.

Builds its own minimal FastAPI app (real PacketCapture + real Database +
a small fake engine) rather than using conftest.py's shared test_app/client
fixtures, which don't wire in packet_capture_router or set
app.state.packet_capture -- same self-contained-harness precedent as
tests/test_stop_suppresses_outbound_traffic.py."""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routers.devices import router as devices_router
from src.api.routers.packet_capture import router as packet_capture_router
from src.bacnet.packet_capture import PacketCapture
from src.db import Database


class _FakeEngine:
    """Exposes both what devices.py needs (async reload(), for
    schedule_engine_reload() on device create) and what packet_capture.py
    needs (resolve_wire_object()) -- no real bacpypes3 Application
    required for these association tests."""

    def __init__(self, wire_map: dict | None = None):
        self._wire_map = wire_map or {}

    async def reload(self) -> None:
        pass

    def resolve_wire_object(self, *, object_type, physical_instance):
        return self._wire_map.get((object_type, physical_instance))


def _build_i_am(device_instance: int) -> bytes:
    obj_id_value = (8 << 22) | device_instance
    apdu = (
        bytes([0x10, 0x00, 0xC4])
        + obj_id_value.to_bytes(4, "big")
        + bytes([0x22, 0x05, 0xC4, 0x91, 0x03, 0x21, 0x0F])
    )
    body = bytes([0x01, 0x00]) + apdu
    return bytes([0x81, 0x0A]) + (4 + len(body)).to_bytes(2, "big") + body


BROADCAST_WHO_IS = bytes.fromhex("81 0b 00 08 01 00 10 08")


@pytest.fixture
def client_factory(tmp_path):
    def _make(*, engine=None, use_database: bool = True):
        app = FastAPI()

        db = None
        if use_database:
            db = Database(tmp_path / "test.db")
            db.setup()

        app.state.db = db
        app.state.engine = engine
        app.state.device_names = {}

        capture = PacketCapture(max_packets=1000)
        capture.start()
        app.state.packet_capture = capture

        app.include_router(devices_router)
        app.include_router(packet_capture_router)

        return TestClient(app), db, capture

    return _make


def test_device_id_filter_returns_only_matching_packets(client_factory):
    client, _db, capture = client_factory(engine=_FakeEngine())

    ahu1 = client.post("/devices", json={"device_instance": 1001, "name": "AHU-1"}).json()
    client.post("/devices", json={"device_instance": 1002, "name": "AHU-2"})

    capture.record_inbound(
        _build_i_am(1001), source=("10.0.0.5", 47810), destination=("255.255.255.255", 47808),
    )
    capture.record_inbound(
        _build_i_am(1002), source=("10.0.0.6", 47810), destination=("255.255.255.255", 47808),
    )

    resp = client.get(f"/packet-capture/packets?device_id={ahu1['id']}")
    assert resp.status_code == 200

    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["simulator_device_id"] == ahu1["id"]


def test_unassociated_filter_excludes_resolved_packets(client_factory):
    client, _db, capture = client_factory(engine=_FakeEngine())

    client.post("/devices", json={"device_instance": 1001, "name": "AHU-1"})

    capture.record_inbound(
        _build_i_am(1001), source=("10.0.0.5", 47810), destination=("255.255.255.255", 47808),
    )
    capture.record_inbound(
        BROADCAST_WHO_IS, source=("10.0.0.9", 47810), destination=("255.255.255.255", 47808),
    )

    resp = client.get("/packet-capture/packets?unassociated=true")
    items = resp.json()["items"]

    assert len(items) == 1
    assert items[0]["simulator_device_id"] is None


def test_pagination_correct_under_device_filter(client_factory):
    # The bug this design's packet_hook-before-pagination approach exists
    # to prevent: a device filter applied AFTER offset/limit slicing would
    # silently return a short/empty page and a wrong `total`.
    client, _db, capture = client_factory(engine=_FakeEngine())

    ahu1 = client.post("/devices", json={"device_instance": 1001, "name": "AHU-1"}).json()
    client.post("/devices", json={"device_instance": 1002, "name": "AHU-2"})

    for _ in range(5):
        capture.record_inbound(
            _build_i_am(1001), source=("10.0.0.5", 47810), destination=("255.255.255.255", 47808),
        )
        capture.record_inbound(
            _build_i_am(1002), source=("10.0.0.6", 47810), destination=("255.255.255.255", 47808),
        )

    resp = client.get(f"/packet-capture/packets?device_id={ahu1['id']}&offset=2&limit=2")
    data = resp.json()

    assert data["total"] == 5
    assert len(data["items"]) == 2
    assert all(item["simulator_device_id"] == ahu1["id"] for item in data["items"])


def test_list_and_detail_return_identical_simulator_fields(client_factory):
    client, _db, capture = client_factory(engine=_FakeEngine())

    ahu1 = client.post("/devices", json={"device_instance": 1001, "name": "AHU-1"}).json()

    capture.record_inbound(
        _build_i_am(1001), source=("10.0.0.5", 47810), destination=("255.255.255.255", 47808),
    )

    list_item = client.get("/packet-capture/packets").json()["items"][0]
    detail = client.get(f"/packet-capture/packets/{list_item['packet_id']}").json()

    for field in (
        "simulator_device_id", "simulator_device_instance", "simulator_device_name",
        "simulator_object_id", "simulator_object_type",
        "simulator_object_instance", "simulator_object_name",
    ):
        assert list_item[field] == detail[field]

    assert list_item["simulator_device_id"] == ahu1["id"]


def test_works_with_no_database_or_engine(client_factory):
    client, _db, capture = client_factory(engine=None, use_database=False)

    capture.record_inbound(
        _build_i_am(1001), source=("10.0.0.5", 47810), destination=("255.255.255.255", 47808),
    )

    resp = client.get("/packet-capture/packets")
    assert resp.status_code == 200

    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["simulator_device_id"] is None
    assert items[0]["service_name"] == "I-Am"  # existing decode still works

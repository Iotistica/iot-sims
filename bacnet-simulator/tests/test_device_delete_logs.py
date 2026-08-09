"""delete_device() (src/api/routers/devices.py) never called log_event(),
unlike create_device()/update_device() and its sibling delete_object() --
so no Activity Log entry ever appeared for a deleted device, even though
every other device/object CRUD action logged one. Fixed by fetching the
device first (for its name/instance in the message) and logging before
the delete, matching delete_object()'s exact pattern."""
from __future__ import annotations


def _create_device(client, *, instance=101, name="Test-Device"):
    resp = client.post("/devices", json={"device_instance": instance, "name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_delete_device_logs_removal(client):
    device_id = _create_device(client)
    client.app.state.logged_events.clear()  # drop the create-time log entry

    resp = client.delete(f"/devices/{device_id}")
    assert resp.status_code == 204

    events = client.app.state.logged_events
    assert len(events) == 1
    assert events[0]["device_id"] == device_id
    assert events[0]["level"] == "warn"
    assert "Test-Device" in events[0]["message"]
    assert "101" in events[0]["message"]


def test_delete_nonexistent_device_returns_404_and_logs_nothing(client):
    client.app.state.logged_events.clear()

    resp = client.delete("/devices/999999")
    assert resp.status_code == 404
    assert client.app.state.logged_events == []

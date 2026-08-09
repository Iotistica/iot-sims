# Read-only BACnet adapter (Python)

Python-side port of the useful parts of the production Node BACnet adapter for
the simulator / digital-twin workflow.

## Deliberate behavior change

The Edge Agent implementation has a guarded BACnet WriteProperty path. This
package does not.

External BACnet is treated as a read-only source:

- discovery: yes
- Who-Is / I-Am: yes
- ReadProperty: yes
- object enumeration: yes
- current-value snapshot: yes
- polling: yes
- source commandability detection: metadata only
- WriteProperty: blocked

`BACnetClient.write()` and `write_property()` fail closed with
`ReadOnlyBACnetError` so accidental callers cannot command production equipment.

## Intended flow

```text
real BACnet network
    -> discover
    -> inspect / validate
    -> create baseline snapshot
    -> create simulator project/scenario
    -> simulate changes on the copy
```

## Integration with the existing simulator

The package is transport-injected. Prefer wrapping the simulator's existing
BACpypes3 `Application` with `Bacpypes3Transport` rather than creating another
BACnet/IP socket:

```python
from src.bacnet.client import (
    BACnetAdapter,
    BACnetAdapterConfig,
    Bacpypes3Transport,
    DiscoveryOptions,
)

transport = Bacpypes3Transport(existing_bacpypes3_application)

adapter = BACnetAdapter(
    BACnetAdapterConfig(),
    transport,
)

snapshot = await adapter.create_network_snapshot(
    DiscoveryOptions(
        discovery_targets=["192.168.1.0/24"],
        timeout_ms=5000,
        max_devices=100,
    )
)
```

For a local broadcast scan, omit `discovery_targets` and configure the
BACpypes3 Application with a subnet/broadcast-capable address.

## Important BACpypes3 note

`Bacpypes3Transport` is intentionally thin because the simulator already owns
the BACpypes3 stack. BACpypes3's official client workflow uses an Application
and async Who-Is / ReadProperty helpers.

The normalizer has been checked against the exact BACpypes3 version pinned by
the simulator (`bacpypes3==0.0.91`, see `requirements.txt`) — `who_is()`'s
`address` argument must be a real `bacpypes3.pdu.Address`, not a plain string;
`read_property()`'s return value is already decoded; `ObjectIdentifier` is a
plain 2-tuple `(type, instance)`. Re-verify these notes if the pinned version
ever changes.

## Files

- `types.py` — device/object/config types, plus the `BACnetTransport` read-only protocol
- `transport.py` — `Bacpypes3Transport`, the wrapper for the simulator's existing BACpypes3 app
- `client.py` — per-device reads, retries, concurrency; writes blocked
- `discovery.py` — Who-Is/I-Am + Object_List validation
- `adapter.py` — high-level discovery/validation and optional polling
- `../../../tests/test_bacnet_readonly_client.py` — verifies reads/normalization work and writes fail closed (lives in the repo's shared `tests/` directory, alongside every other test)

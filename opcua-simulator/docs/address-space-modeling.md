# OPC UA address-space modeling: current mapping and a known, deferred correction

This document records a confirmed modeling gap in how this simulator
represents devices in the OPC UA address space, why it exists, and the
intended fix — deliberately **not** part of the folder-hierarchy feature
(see the "Folder Hierarchy (Phase 1)" plan). Ships separately, later, as its
own explicit compatibility change.

## Current mapping (confirmed from code, not assumed)

Every simulated device is created as:

- **NodeClass:** `Object`
- **TypeDefinition:** `FolderType` (`i=61`)

Source: `NodeManager.create_device()` in `lib/nodes.py` calls
`parent.add_folder(...)`, which traces through asyncua's
`create_folder()` → `_create_object(..., ua.ObjectIds.FolderType)`
(`asyncua/common/manage_nodes.py`) — the same helper, and the same
resulting node type, used for pure organizational folders (`Factory`,
`Pumping Station`, etc. — see the folder-hierarchy feature).

In other words: **a device and a plain organizational folder are currently
indistinguishable by type.** Browsing the address space, a client sees
`Pump-1` with exactly the same `TypeDefinition` as `Factory`.

## Why this is non-ideal

The base OPC UA specification doesn't forbid using `FolderType` for
anything that organizes children — it's not a protocol violation. But it
diverges from standard modeling convention (e.g. the OPC UA Device
Integration, "DI", companion specification's device-instance pattern),
where:

- `FolderType` is reserved for pure organizational grouping with no
  identity of its own.
- A physical/logical device instance is modeled as an `Object` — typically
  a `DeviceType` subtype — exposing its data as components via
  `HasComponent`.

The practical consequence: **any OPC UA client that discovers devices by
filtering for a non-Folder Object type (or a `DeviceType` subtype,
per the DI convention) will not recognize `Pump-1` as a device at all.**
It's indistinguishable by type from a pure grouping folder like `Factory`.

## Why it isn't being fixed in this pass

Changing `create_device()` to use `add_object()` (`TypeDefinition =
BaseObjectType`, or a project-defined `DeviceType`) instead of
`add_folder()` is a **breaking change to the live address space of every
existing project** — any client that currently browses a device and sees
`TypeDefinition=FolderType` would see something else after the fix. That
needs to be a deliberate, visible, opt-in change — not something a user
discovers after the fact because it happened to ship bundled with an
unrelated feature (folder support).

## Intended correction (future work, not yet implemented)

- `NodeManager.create_device()` switches to `add_object()`, with
  `TypeDefinition` either `BaseObjectType` or a purpose-built `DeviceType`
  registered once at server startup (open question: is a full custom
  `DeviceType` — with proper `HasTypeDefinition`/`HasSubtype` modeling —
  worth the added complexity over just `BaseObjectType`, for a simulator
  whose purpose is generating tag data rather than hosting a faithful
  companion-spec type hierarchy?).
- Shipped gated behind an explicit project-level compatibility setting/
  migration, not a silent default flip — existing projects keep today's
  `FolderType` behavior until a user deliberately opts in, at which point
  the live address space is rebuilt with the corrected type.
- Because device NodeIds are now id-based (`device/<id>`, see the
  folder-hierarchy feature), this correction is a **pure type/attribute
  change** — no NodeId churn on top of it, unlike the earlier path-based
  NodeId scheme this simulator used before that change shipped.

## What the folder-hierarchy feature deliberately does *not* touch

Folders (`Factory`, `Pumping Station`, etc.) are correctly `FolderType` —
that part of the model was always right. Only **Device** has the
type-mapping gap described above. The folder feature adds a real,
type-correct container above devices; it does not attempt to fix devices'
own type in the same pass.

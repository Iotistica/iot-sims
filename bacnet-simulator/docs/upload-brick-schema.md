# Plan: Let users import a Brick Schema (.ttl) file to classify their project

Repo: `C:\Users\Dan\iot-sims\bacnet-simulator`

> This plan file previously held the (now fully implemented and shipped) Simulation Model
> topology-scoping bug fix. This is a new, distinct task — replacing that content entirely.

## Context

The app already exports a project's/device's semantic model as a Brick Schema Turtle file
(`src/bacnet/brick_export.py`), but there's no way back in — a user who has a Brick file (from a
previous export, hand-authored, or produced by another tool) can't apply it to (re)classify their
project. This adds self-service Brick **import**: upload a `.ttl` file and have it apply
Locations, Equipment classification, point classification, and Brick relationships
(`feeds`/`controls`/`isPartOf`/`hasLocation`/`isPointOf`) to the current live project.

**Scope, confirmed with the user**: semantic layer only. Import matches the file's BACnet
`device-instance`/`object-identifier` references against devices/points that **already exist**
in the live project; it creates/updates Locations, Equipment, classifications, and relationships,
but never fabricates new BACnet devices or objects. A device/point the file references that isn't
in the project is skipped and reported, not invented. This is deliberately the mirror image of
what `brick_export.py` already emits — an export → edit → re-import round-trip is the primary
use case this is grounded in, not "build a building from scratch via Brick."

## Key facts established (from direct exploration this session)

- **No Brick import code exists anywhere** — confirmed by exhaustive grep. `rdflib.Graph().parse(...)`
  is used nowhere in the codebase; this feature is the first consumer of rdflib's parse side
  (only `Graph()`-build + `.serialize()` are used today, in `brick_export.py`). `requirements.txt`
  pins `rdflib>=7.0` (floating minimum) — worth pinning exact before depending on parse behavior.
- **`brick_export.py`'s emission shape is the exact schema to parse back**, already fully known:
  `PREDICATE_TO_BRICK` (`brick_export.py:93-100`, forward/inverse pairs for `isPointOf`/`isPartOf`/
  `feeds`/`hasLocation`/`controls`/`isHostedBy`) — **reuse this dict directly, inverted, rather
  than duplicating the predicate/Brick-term mapping**. Device/object nodes carry a
  `ref:hasExternalReference` blank node with `bacnet:object-identifier` ("<type>,<instance>") and
  the device's own `bacnet:device-instance` literal — this is the BACnet-identity key to match
  existing rows against. Equipment/Location/Controller nodes are plain `RDFS.label` + `RDF.type
  BRICK[brick_class]`, checked against `EQUIPMENT_TYPES`/`LOCATION_KINDS`/`CONTROLLER_TYPES`/
  `POINT_TYPES` (`src/core/config.py`) exactly like export does.
- **Existing matching/upsert conventions to reuse, not reinvent**:
  - `Database.sync_external_devices` (`database.py:1373-1416`) is the closest precedent for
    "reconcile external structured data against existing rows": keyed on `devices.device_instance`
    (globally `UNIQUE`), `INSERT ... ON CONFLICT DO UPDATE`, **never deletes** on a missing match.
    Mirror this idempotent-reconciliation shape for devices/objects matching.
  - `Database.import_ede_objects` (`database.py:1675-1690`) upserts on `objects`'s real
    `UNIQUE(device_id, object_type, object_instance)` constraint — same key to match Brick's
    `bacnet:object-identifier` against.
  - **No `get_device_by_instance()` helper exists yet** — needed new, small addition (only
    `get_device(id)` by surrogate key exists today).
  - `locations`/`equipment` have **no UNIQUE constraint at all** — no DB-enforced identity. Match
    these by exact `name` within the live project (app-level, not DB-enforced): found → reuse and
    update its classification; not found → create. This is a deliberate, explicit, simple rule
    (not a graph-merge system).
  - `semantic_key` (`src/semantics/keys.py`) must always be **recomputed from freshly-resolved
    live ids** (`derive_semantic_key(entity_kind, brick_class, device_id=..., object_id=...,
    location_id=..., equipment_id=..., local_slug=...)`), never trusted from the TTL file's own
    blank-node/URI identity — same rule `load_project()`'s restore logic already follows.
  - `create_semantic_relationship`/`upsert_semantic_relationship` are already idempotent
    (`UNIQUE(source_entity_id, predicate, target_entity_id)`, `ON CONFLICT DO NOTHING`) — reuse
    directly for relationship writes, no new dedup logic needed there.
- **No reusable bulk-write `Database` method exists** for `semantic_entities`/
  `semantic_relationships` — `load_project()`'s bulk restore loop is private/inline, not a public
  method. This importer needs its own small set of writes; looping the existing single-row
  `create_location`/`update_location`/`create_equipment`/`update_equipment` (which already trigger
  `sync_entity_from_flat_field` classification-mirroring for free) plus
  `create_semantic_relationship` is the right level — no new bulk-insert machinery, counts here
  are small (tens to low hundreds of nodes/edges per file, not thousands).
- **File-upload convention already established**: `uploadFile<T>(path, file, fields, markDirty)`
  (`admin/src/api.ts:157-169`) — builds `FormData`, POSTs, throws on non-OK. Reuse directly for
  the `.ttl` upload; no new upload helper needed.
- **Preview-before-apply is a deliberate deviation from this codebase's house style** (EDE/
  template imports apply unconditionally after a plain `Modal.confirm` warning). Given this is
  rdflib's first real-world parse exercise (no dialect-quirk precedent to lean on) and the blast
  radius is project-wide classification, a dry-run preview step is warranted here specifically —
  called out explicitly since it's not "just following existing convention."
- Both a device-scoped (`GET /devices/{id}/export/brick`) and project-scoped
  (`GET /profiles/{id}/export/brick`) Brick export already exist in `src/api/routers/exports.py` —
  but the project one exports a **saved profile's stored snapshot**, not live state. Import is the
  opposite: it must operate on **live state directly** (same as Assign Points/EquipmentDrawer/etc.
  already do), not create a new saved-profile catalog entry the way `POST /profiles/import`
  (project-JSON/EDE) does — those two are a genuinely different paradigm from what's needed here.

## Design

### 1. Backend: `src/bacnet/brick_import.py` (new, mirrors `brick_export.py`'s independence)

- `parse_brick_ttl(text: str) -> rdflib.Graph` — thin wrapper around `Graph().parse(data=text,
  format="turtle")`, converting rdflib parse errors into a clear `ValueError`/`HTTPException`-friendly
  message (invalid Turtle syntax is the primary failure mode to handle gracefully).
- `build_import_plan(graph, database) -> BrickImportPlan` (the **preview** step, read-only):
  walks the graph's device/object/equipment/location nodes (via `ref:hasExternalReference` +
  `bacnet:object-identifier`/`device-instance` for BACnet-identity matching, via `RDF.type` against
  `EQUIPMENT_TYPES`/`LOCATION_KINDS`/`CONTROLLER_TYPES`/`POINT_TYPES` for classification), and the
  six relationship predicates via `PREDICATE_TO_BRICK` inverted. Resolves each node against live
  `Database` state (`get_device_by_instance`, object lookup by `(device_id, object_type,
  object_instance)`, equipment/location lookup by name) and produces a plan dataclass:
  `{locations_to_create, locations_to_update, equipment_to_create, equipment_to_update,
  points_to_classify, relationships_to_create, unresolved: list[{node, reason}]}` — every count
  and every skipped item explained, for the preview UI.
- `apply_import_plan(database, plan) -> BrickImportResult` (the **apply** step): executes the plan
  via the existing single-row `Database` methods named above, in FK-safe order (locations →
  equipment → point classification → relationships), returns created/updated counts.

### 2. `Database` additions (`src/db/database.py`)

- `get_device_by_instance(device_instance: int) -> Optional[dict]` — small, new; mirrors
  `get_device(id)`'s shape, queried against the real `UNIQUE` column.
- A small object-lookup helper (`(device_id, object_type, object_instance) -> Optional[dict]`) if
  one doesn't already cleanly exist for reuse (the EDE upsert path relies on the SQL constraint
  directly rather than a lookup method — this importer's preview step needs an actual read).

### 3. API routes — new small router `src/api/routers/brick_import.py`

- `POST /brick-import/preview` — multipart `.ttl` upload, calls `parse_brick_ttl` +
  `build_import_plan`, returns the plan as JSON (read-only, nothing written).
- `POST /brick-import/apply` — multipart `.ttl` upload (frontend re-sends the same file rather
  than the app inventing a server-side staged-plan cache — keeps this stateless and simple),
  re-parses + re-builds the plan, then `apply_import_plan`, returns the result counts.

### 4. Frontend — new `admin/src/components/BrickImportModal.vue`

- File picker (`.ttl`) using the existing `uploadFile` helper (`admin/src/api.ts`).
- On file select: POST to preview, render a summary (Locations to create/update, Equipment to
  create/update, points to classify, relationships to create, and the unresolved list with
  reasons) — mirrors `TemplatePickerModal.vue`'s confirm-before-overwrite framing.
- "Apply" button: POST to apply, then reload `locations`/`equipment`/`devices` (+ the
  Browse-tree's `controllerEquipmentMap`, per the earlier topology-nesting work) via `App.vue`'s
  existing loaders, toast success/failure.
- Entry point: a new "Import Brick Schema" action alongside the existing "Open Project"/"New
  Project" project-level actions in `App.vue`'s header (live-project-wide operation, not
  per-device — matches Brick's own cross-entity scope), opening this modal.

### Explicitly out of scope (per the confirmed decision)

- No fabrication of new BACnet devices/objects from the Brick file — unmatched device/object
  references are reported as unresolved, never invented.
- No new saved-profile/catalog entry (unlike `POST /profiles/import`) — this mutates live state.
- No generic RDF/Brick reasoning engine, no SHACL validation, no support for Brick vocabulary
  beyond the six relationship predicates and the canonical class vocabularies already used by
  export.
- No changes to `brick_export.py` itself.

## Files to change

- `src/bacnet/brick_import.py` — new (parse + plan-build + apply).
- `src/db/database.py` — `get_device_by_instance` + object-lookup helper additions.
- `src/api/routers/brick_import.py` — new router (`preview`/`apply`), registered alongside the
  other routers (`src/application.py`/router registration list).
- `admin/src/components/BrickImportModal.vue` — new.
- `admin/src/App.vue` — entry point wiring + reload-after-apply.
- `admin/src/api.ts` — `brickImport.preview`/`brickImport.apply` client methods (reusing
  `uploadFile`).

## Tests

- Backend: new `tests/test_brick_import.py`, following `tests/test_brick_export.py`'s
  established fixture conventions (build a small live project via the existing routers, export it
  to Turtle with the existing `build_brick_graph`/`graph_to_ttl`, then round-trip: import that
  exact Turtle back in and assert the plan/apply results match what was exported — the strongest,
  most natural correctness test available). Add targeted cases for: a device/object referenced in
  the file that doesn't exist in the project (reported unresolved, nothing invented), an
  Equipment/Location matched by existing name (updated, not duplicated) vs. no match (created),
  and malformed Turtle input (clear error, not a 500).
- No frontend test framework exists in this repo (confirmed in earlier session work) — frontend
  verification is type-check/build plus a manual pass.

## Verification

1. Run the new backend tests plus `tests/test_brick_export.py`, `tests/test_semantic_api.py`,
   `tests/test_equipment_feeds_relationships.py` to confirm no regression in the layers this
   builds on.
2. `python -m py_compile` the new/touched backend files.
3. `npx vue-tsc --noEmit` / `npm run build` in `admin/`.
4. Manual round-trip pass (yours to run): export a project's Brick file, tweak a classification or
   relationship in it, re-import via the new modal, confirm the preview correctly shows what will
   change, and confirm Apply produces the expected Location/Equipment/relationship state without
   touching any BACnet device/object identity.

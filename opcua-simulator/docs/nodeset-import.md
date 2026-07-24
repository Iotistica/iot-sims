# OPC UA NodeSet2 XML import

Lets you import a real vendor's [NodeSet2 XML](https://reference.opcfoundation.org/Core/Part6/v104/docs/F.2)
address-space export and simulate it, instead of hand-building a profile
from scratch. This is a first pass — see **Scope** and **Known limitations**
below for what's deliberately not here yet (export, behavior suggestions,
nested live objects).

## Architecture decision: adapter onto devices/tags, not a parallel model

A real vendor NodeSet, once you strip `UAObjectType`/`UAVariableType`/
`UADataType` scaffolding, is almost always a shallow tree of Objects
containing Variables — exactly what this simulator's existing `devices`/
`tags` SQLite schema already models (see `lib/db.py`). So instead of adding
a second, generalized "canonical node" table alongside it, the importer
(`lib/nodeset/importer.py`) is an **adapter**: it turns a parsed NodeSet
into `devices`/`tags` rows using the same tables, `NodeManager`, and
behavior engine every hand-created device already uses. Nothing else in the
app needs to know an import happened.

The cost of that choice: nested Object hierarchy inside a device is
flattened into dotted tag names (`SubAssembly.Motor.Current`) rather than
modeled as nested live OPC UA objects, and original source NodeIds/
namespace indices are not preserved — imported tags get the same
deterministic name-derived NodeIds any manually-created tag gets. That
means no byte-for-byte round-trip export (yet). Given this simulator's
actual use case — simulate a device's *tags*, not host a faithful copy of
someone else's type system — that trade was judged worth it. See **Known
limitations** for the honest list.

## Scope

Supported node classes: `UAObject`, `UAVariable`. `UAMethod`,
`UAObjectType`, `UAVariableType`, `UADataType`, `UAReferenceType` are parsed
far enough to be counted and reported in `unsupported_features` — never
silently dropped — but not deeply modeled or imported as tags/devices.

Supported hierarchy references: `Organizes`, `HasComponent`, `HasProperty`
(either direction — a child's inverse reference or a parent's forward one,
both are legal NodeSet2 encodings real exporters use interchangeably).

Supported DataTypes, coerced onto the simulator's four native types
(`Boolean`, `Double`, `Int32`, `String`):

| NodeSet2 DataType | Simulator type | Notes |
|---|---|---|
| Boolean | Boolean | |
| SByte, Byte, Int16, UInt16, Int32 | Int32 | |
| UInt32, Int64, UInt64 | Int32 | narrowed — values outside Int32's range clip, reported as a warning |
| Float, Double | Double | |
| String, DateTime, QualifiedName, LocalizedText | String | |
| anything else (Guid, ByteString, a custom `UADataType`, enums, structures) | String | reported as a warning, value preserved as its string form where derivable |

Scalar `<Value>` types are parsed (Boolean/numeric/String/DateTime/
LocalizedText/QualifiedName). Array values (`<uax:ListOf...>`) are detected
and reported but not carried into the tag — it's created with a
type-appropriate default value instead, since the simulator has no array
tag type to hold one.

Every imported variable gets `manual` behavior, seeded with its parsed
source value (or a type-appropriate default if none was present/parseable).
This first pass intentionally skips a confidence-scored behavior-suggestion
engine (heuristics like "name contains 'temperature' → suggest a sine
wave") — the existing tag-edit UI already lets you change any imported
tag's behavior afterward, so nothing is locked in, it just doesn't guess.

## Import workflow

1. **Preview** (`POST /nodesets/preview`) — parses and validates the file,
   returns the parse report plus a plan summary (which devices/tags would
   be created). Nothing is written to the database.
2. **Import** (`POST /nodesets/import`) — re-parses, re-plans (preview and
   import share the same `plan_import()` so they can never disagree), then
   commits: all device/tag rows are inserted in a single SQLite transaction
   (all-or-nothing), then live OPC UA nodes are created for what was
   inserted. If live-node creation fails partway through, the live address
   space is rebuilt from the now-committed DB state (`SimEngine.
   rebuild_live_state()`) so it can't silently diverge from the database.
3. **Import history** — `GET /nodesets/imports` lists batches, `GET
   /nodesets/imports/{id}` gets one, `DELETE /nodesets/imports/{id}` removes
   only the devices that batch created (and their tags, via cascade) — a
   device already deleted independently since the import is skipped, not
   treated as an error, and reported back as `already_removed`.

### Device/tag placement rule

* Every top-level `UAObject` (its parent isn't present in the document —
  nothing to attach it under) becomes one device.
* Every `UAVariable` found anywhere beneath it becomes one tag on that
  device. Nesting is flattened into the tag name (`Conveyor1.Speed`).
* Bare top-level `UAVariable`s (no enclosing Object at all) land on one
  catch-all device named after the import, so nothing is dropped just
  because it doesn't fit the device/tag assumption.

### Conflict strategies

Only two of the four commonly-discussed strategies are implemented in this
pass:

* `skip` (default) — a planned device whose name already exists in the DB
  is left alone; its creation is reported under `devices_skipped`.
* `reject` — if *any* planned device name already exists, the whole import
  fails with no changes made.

`replace` (overwrite an existing device's tags) and `remap` (auto-rename on
collision) are **not implemented** — both need more careful semantics
(safely overwriting live nodes; stable dedup identity) than this pass
covers. Rename the conflicting device first, or use `reject`/`skip`.

## Security limits

* XML is parsed with `defusedxml`, which rejects DTDs, external entities,
  and external references outright (XXE, billion-laughs) rather than
  silently ignoring them — a rejected file raises `NodeSetParseError` before
  any content is processed.
* File size is capped at `NODESET_MAX_FILE_SIZE_BYTES` (default 10 MB),
  checked before the file is handed to the XML parser.
* Parsed node count is capped at `NODESET_MAX_NODE_COUNT` (default 50,000)
  — a large-but-well-formed file is a cost DoS risk `defusedxml` alone
  doesn't cover.
* The uploaded file's raw content is never logged — only filename, size,
  and derived counts.
* All `/nodesets/*` endpoints require the same bearer-token auth as every
  other management endpoint (see the `auth_gate` middleware) — there's no
  separate public preview path.

## Known limitations

* **No export yet.** Only import is implemented in this pass.
* **No round-trip.** Original NodeIds and namespace indices aren't
  preserved in the live address space — see the architecture note above.
* **Nested Objects are flattened**, not represented as nested live OPC UA
  objects — a client browsing the simulator will see one flat device folder
  with dotted tag names, not the source's original object tree.
* **`replace`/`remap` conflict strategies are not implemented.**
* **No behavior-suggestion engine** — everything imports as `manual`.
* **Array-valued tags are not supported** — the array is detected and
  reported, the tag is created with a default value instead.
* **Custom/complex DataTypes** (enums, structures, Guid, ByteString) are
  coerced to `String`, losing their original typed structure.

## API examples

```bash
# Preview — no changes made
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@machine.xml" \
  http://localhost:47901/nodesets/preview

# Commit the import
curl -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@machine.xml" \
  -F "name=Packaging Line PLC" \
  -F "conflict_strategy=skip" \
  http://localhost:47901/nodesets/import

# Import history
curl -H "Authorization: Bearer $TOKEN" http://localhost:47901/nodesets/imports

# Remove everything a specific import created
curl -X DELETE -H "Authorization: Bearer $TOKEN" http://localhost:47901/nodesets/imports/1
```

## Troubleshooting

| Symptom | Cause |
|---|---|
| `400 Rejected unsafe XML construct` | File has a DTD/external entity — not a bug, this is the XXE guard working. Strip the `<!DOCTYPE ...>` block if it's not actually needed. |
| `400 Root element is <X>, expected <UANodeSet>` | Not a NodeSet2 XML file, or it's wrapped in something else. |
| `413 File exceeds the N-byte limit` | Raise `NODESET_MAX_FILE_SIZE_BYTES` if the file is legitimately large. |
| A tag shows an empty/default value after import | Its source `DataType` wasn't one of the built-in scalar types, or its `<Value>` was an array — check `warnings` in the import response for why. |
| `409` on import with `conflict_strategy=reject` | A device with that name already exists — rename it first or use `skip`. |

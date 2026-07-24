"""
Adapter that turns a parsed NodeSet2 document into the simulator's existing
devices/tags schema (lib/db.py), and drives live OPC UA node creation.

Architecture choice (see docs/nodeset-import.md for the full rationale): this
deliberately does NOT introduce a parallel generalized "canonical node" table
alongside devices/tags. A real vendor NodeSet is, once you strip
UAObjectType/UAVariableType/UADataType scaffolding, almost always a shallow
tree of Objects containing Variables — exactly what devices/tags already
model. So the adapter:

  * Every top-level Object (parent not present in the document, i.e. nothing
    to attach it under) becomes one device.
  * Every Variable found anywhere beneath it becomes one tag on that device.
    Intermediate Object nesting is flattened into a dotted tag name
    ("SubAssembly.Motor.Current") rather than modeled as nested live nodes —
    this is the one real structural loss of this phase, called out explicitly
    rather than silently dropped.
  * Bare top-level Variables (no enclosing Object at all) land on one
    catch-all device named after the import, so nothing is dropped just
    because it doesn't fit the device/tag assumption.

Original source NodeIds/namespace indices are not preserved in the live
address space — imported tags get the same deterministic name-derived
NodeIds any manually-created tag gets (lib/nodes.py). That means no
byte-for-byte round-trip export yet; it's a real, documented limitation, not
an oversight (see the module docstring in models.py and the design note in
docs/nodeset-import.md).
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Optional

from .mapping import coerce_data_type, coerce_initial_value, default_behavior_for
from .models import ImportedNode, ParsedNodeSet

logger = logging.getLogger("opcua-sim.nodeset")

OBJECTS_FOLDER_ID = "i=85"


@dataclass
class PlannedTag:
    source_node_id: str
    name: str
    data_type: str
    writable: bool
    behavior: str
    behavior_params: str


@dataclass
class PlannedDevice:
    source_node_id: Optional[str]
    name: str
    description: str
    tags: list[PlannedTag] = field(default_factory=list)


@dataclass
class ImportPlan:
    devices: list[PlannedDevice]
    warnings: list[str] = field(default_factory=list)


@dataclass
class ImportResult:
    import_id: Optional[int]
    devices_created: list[dict]
    tags_created: int
    devices_skipped: list[str]
    warnings: list[str]
    errors: list[str]


def _browse_local_name(browse_name: str) -> str:
    """NodeSet2 BrowseNames are QualifiedNames written as "<ns_index>:<name>"
    (e.g. "1:Temperature"). The index isn't meaningful once flattened into a
    device/tag name, so drop it."""
    if ":" in browse_name and browse_name.split(":", 1)[0].isdigit():
        return browse_name.split(":", 1)[1]
    return browse_name


def plan_import(parsed: ParsedNodeSet, batch_name: str) -> ImportPlan:
    """Pure function: decide device/tag placement without touching the DB or
    live server. Preview and commit both call this so they can never
    disagree about what an import will do."""
    nodes = parsed.nodes
    warnings: list[str] = []

    children_by_parent: dict[str, list[ImportedNode]] = {}
    for node in nodes.values():
        if node.parent_node_id:
            children_by_parent.setdefault(node.parent_node_id, []).append(node)

    top_level_objects = [
        n for n in nodes.values()
        if n.node_class == "Object" and (n.parent_node_id is None or n.parent_node_id not in nodes)
    ]
    top_level_variables = [
        n for n in nodes.values()
        if n.node_class == "Variable" and (n.parent_node_id is None or n.parent_node_id not in nodes)
    ]

    devices: list[PlannedDevice] = []

    for obj in top_level_objects:
        device_name = _browse_local_name(obj.display_name or obj.browse_name)
        device = PlannedDevice(
            source_node_id=obj.node_id,
            name=device_name,
            description=obj.description or "",
        )
        _collect_tags(obj.node_id, [], children_by_parent, device, warnings)
        devices.append(device)

    if top_level_variables:
        catch_all = PlannedDevice(
            source_node_id=None,
            name=batch_name or "Imported NodeSet",
            description="Variables with no enclosing Object in the source document",
        )
        for var in top_level_variables:
            _add_tag(var, [], catch_all, warnings)
        devices.append(catch_all)

    return ImportPlan(devices=devices, warnings=warnings)


def _collect_tags(
    node_id: str, path_prefix: list[str],
    children_by_parent: dict[str, list[ImportedNode]],
    device: PlannedDevice, warnings: list[str],
    _depth: int = 0,
) -> None:
    if _depth > 32:
        warnings.append(f"{node_id}: hierarchy nesting too deep (>32), stopped descending")
        return
    for child in children_by_parent.get(node_id, []):
        if child.node_class == "Variable":
            _add_tag(child, path_prefix, device, warnings)
        elif child.node_class == "Object":
            local = _browse_local_name(child.display_name or child.browse_name)
            _collect_tags(child.node_id, path_prefix + [local], children_by_parent, device, warnings, _depth + 1)
        # Methods/other node classes under an Object are counted at parse
        # time (ParseReport) but intentionally not represented as tags.


def _add_tag(var: ImportedNode, path_prefix: list[str], device: PlannedDevice, warnings: list[str]) -> None:
    local = _browse_local_name(var.display_name or var.browse_name)
    tag_name = ".".join(path_prefix + [local]) if path_prefix else local

    # coerce_data_type/coerce_initial_value append to a ParseReport normally;
    # this path runs after parsing, so route their warnings into the plan's
    # own warning list via a throwaway shim report.
    from .models import ParseReport
    shim = ParseReport()
    sim_type = coerce_data_type(var.data_type, var.node_id, shim)
    value = coerce_initial_value(var.initial_value, sim_type)
    behavior, behavior_params = default_behavior_for(sim_type, value)
    warnings.extend(shim.warnings)

    device.tags.append(PlannedTag(
        source_node_id=var.node_id,
        name=tag_name,
        data_type=sim_type,
        writable=var.writable,
        behavior=behavior,
        behavior_params=behavior_params,
    ))


async def commit_import(
    db, engine, plan: ImportPlan, *, source_filename: str, conflict_strategy: str = "skip",
) -> ImportResult:
    """Persist the plan (one DB transaction — all-or-nothing for the parts
    that don't conflict) then build live OPC UA nodes for what was created.
    conflict_strategy: "skip" (default) leaves existing same-name devices
    alone and reports them skipped; "reject" fails the whole import if any
    planned device name already exists. ("replace"/"remap" from the fuller
    spec are deferred — see docs/nodeset-import.md.)
    """
    if conflict_strategy not in ("skip", "reject"):
        raise ValueError(f"Unsupported conflict_strategy '{conflict_strategy}' in this phase")

    existing_names = {d["name"] for d in await asyncio.to_thread(db.get_devices)}
    to_create = []
    skipped = []
    for planned in plan.devices:
        if planned.name in existing_names:
            if conflict_strategy == "reject":
                return ImportResult(
                    import_id=None, devices_created=[], tags_created=0,
                    devices_skipped=[], warnings=plan.warnings,
                    errors=[f"Device '{planned.name}' already exists (conflict_strategy=reject)"],
                )
            skipped.append(planned.name)
            continue
        to_create.append(planned)

    if not to_create:
        return ImportResult(
            import_id=None, devices_created=[], tags_created=0,
            devices_skipped=skipped, warnings=plan.warnings, errors=[],
        )

    db_devices = await asyncio.to_thread(_insert_plan_transactional, db, to_create)

    tags_created = sum(len(d["tags"]) for d in db_devices)
    import_id = await asyncio.to_thread(
        db.create_nodeset_import, source_filename, [d["id"] for d in db_devices],
        len(db_devices), tags_created, len(plan.warnings),
    )

    try:
        # engine.add_device_live()/add_tag_live() each acquire structural_lock
        # themselves (asyncio.Lock isn't reentrant), so a whole-batch import
        # calls their lock-free internals directly under one acquisition
        # instead — see SimEngine._add_device_live_locked / _create_live_tag.
        async with engine.structural_lock:
            for d in db_devices:
                await engine._add_device_live_locked(d)
                for t in d["tags"]:
                    if t["enabled"]:
                        await engine._create_live_tag(d["id"], t)
    except Exception:
        logger.exception(
            "Live node creation failed mid-import (import_id=%s) — rebuilding live "
            "address space from persisted DB state to avoid divergence", import_id,
        )
        async with engine.structural_lock:
            await engine.rebuild_live_state()
        plan.warnings.append(
            "Live OPC UA node creation hit an error partway through — the address space was "
            "rebuilt from the database, so all imported devices are present, but check logs for "
            "the underlying cause."
        )

    return ImportResult(
        import_id=import_id,
        devices_created=db_devices,
        tags_created=tags_created,
        devices_skipped=skipped,
        warnings=plan.warnings,
        errors=[],
    )


def _insert_plan_transactional(db, planned_devices: list[PlannedDevice]) -> list[dict]:
    """Runs inside asyncio.to_thread — plain sqlite3, single connection/transaction
    for the whole plan (mirrors Database.replace_live_state's existing pattern),
    so a mid-import DB failure leaves zero partial devices behind."""
    with db._conn() as conn:
        result = []
        for planned in planned_devices:
            key = db._unique_key(conn, planned.name)
            cur = conn.execute(
                "INSERT INTO devices (key, name, description, manufacturer, model, enabled) "
                "VALUES (?,?,?,?,?,1)",
                (key, planned.name, planned.description, "", ""),
            )
            device_id = cur.lastrowid
            tag_rows = []
            for tag in planned.tags:
                tcur = conn.execute(
                    "INSERT INTO tags (device_id, name, data_type, writable, unit, behavior, "
                    "behavior_params, enabled) VALUES (?,?,?,?,?,?,?,1)",
                    (device_id, tag.name, tag.data_type, 1 if tag.writable else 0, "",
                     tag.behavior, tag.behavior_params),
                )
                tag_rows.append(dict(conn.execute(
                    "SELECT * FROM tags WHERE id=?", (tcur.lastrowid,)
                ).fetchone()))
            device_row = dict(conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone())
            device_row["tags"] = tag_rows
            result.append(device_row)
        conn.commit()
        return result

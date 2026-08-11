"""Brick Schema (Turtle/TTL) export for the BACnet simulator.

Independent from ede.py — no shared code, only the same route/response
shape convention (see the "# -- EDE import/export --" section in
simulator.py for the precedent this mirrors).

The BACnet-integration pattern below (bacnet:BACnetDevice +
bacnet:device-instance on the device, and a ref:hasExternalReference blank
node per point carrying bacnet:object-identifier/object-name/objectOf) is
taken directly from BrickSchema/Brick's own pinned v1.4.4 example
(examples/bacnet/bacnet.ttl) and ref extension (support/ref-schema.ttl) —
not invented. See src/config.py's EQUIPMENT_TYPES/POINT_TYPES comment for
the same "verify against the pinned release" rule applied here.

Equipment-to-equipment relationships (brick:feeds/hasPart/isPartOf) are
real, verified Brick predicates, but this simulator has no data model for
equipment relationships today (no feeds/hasPart anywhere in the schema) —
so exporting them is a no-op for now, not a gap in this module.

Each device node is asserted as BOTH bacnet:BACnetDevice and its Brick
equipment class (e.g. brick:Air_Handling_Unit) on the same URI. Real Brick
modeling distinguishes the controller/device that *hosts* points from the
equipment those points semantically describe (newer Brick has separate
hosting relationships for the former, while hasPoint/isPointOf is about
the latter) — but this simulator has exactly one device per equipment
today, so merging them onto one entity is a deliberate v1 simplification,
not an oversight. Revisit if/when a single device can host points for
multiple distinct pieces of equipment.

Building the semantic model as an rdflib.Graph (rather than hand-rolling
Turtle text) means future formats (JSON-LD, RDF/XML, N-Triples, ...) are a
one-line `graph.serialize(format=...)` away on the exact same graph —
see graph_to_ttl() below for the only format-specific piece.
"""
from __future__ import annotations

from typing import Optional

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD

from ..core.config import CONTROLLER_TYPES, EQUIPMENT_TYPES, LOCATION_KINDS, POINT_TYPES

BRICK = Namespace("https://brickschema.org/schema/Brick#")
REF = Namespace("https://brickschema.org/schema/Brick/ref#")
BACNET = Namespace("http://data.ashrae.org/bacnet/2020#")
QUDT_UNIT = Namespace("http://qudt.org/vocab/unit/")

# BACnet unit name -> QUDT unit local name. Verified against Brick's own
# bundled support/VOCAB_QUDT-UNITS-ALL.ttl at v1.4.4 (checked each entry's
# rdfs:label, not guessed from the name) rather than assumed. "no-units"
# has no entry — hasUnit is only emitted for points with a real physical
# unit, matching how Brick's own examples only tag units where one applies
# (never on status/command points).
UNIT_TO_QUDT = {
    "degrees-celsius": "DEG_C",
    "degrees-fahrenheit": "DEG_F",
    "degrees-kelvin": "K",
    "percent": "PERCENT",
    "parts-per-million": "PPM",
    "kilowatts": "KiloW",
    "watts": "W",
    "kilowatt-hours": "KiloW-HR",
    "amperes": "A",
    "volts": "V",
    "cubic-feet-per-minute": "FT3-PER-MIN",
    "liters-per-second": "L-PER-SEC",
    "pascals": "PA",
    "kilopascals": "KiloPA",
    "bars": "BAR",
    "cubic-meters-per-hour": "M3-PER-HR",
    "revolutions-per-minute": "REV-PER-MIN",
    "meters-per-second": "M-PER-SEC",
    "luxes": "LUX",
}

# Brick Core semantic relationships (semantic_entities/semantic_relationships,
# src/legacy.py's Database.setup() + src/semantics/) -> (forward, inverse)
# Brick predicate pairs. All four verified against the pinned v1.4.4
# bricksrc/relationships.py, same "confirm against the real source" rule as
# EQUIPMENT_TYPES/POINT_TYPES above -- see src/core/config.py.
#
# controls/isHostedBy are a narrow, isolated exception to the v1.4.4 rule
# above -- verified against the real (release-candidate, not yet stable)
# tag v1.5.0-rc1's bricksrc/relationships.py specifically, since neither
# predicate exists yet at the pinned v1.4.4. Not a project-wide version
# bump -- see src/core/config.py's SEMANTIC_PREDICATES comment for the
# full rationale and the storage-direction rule these two follow (store
# whichever entity matches the *named* predicate's real rdfs:domain):
# "controls" (domain=Controller) stores Controller-as-source; "isHostedBy"
# (domain=Point -- it's the inverse of "hosts", whose domain is
# Controller) stores Point-as-source.
PREDICATE_TO_BRICK = {
    "isPointOf": (BRICK.isPointOf, BRICK.hasPoint),
    "isPartOf": (BRICK.isPartOf, BRICK.hasPart),
    "feeds": (BRICK.feeds, BRICK.isFedBy),
    "hasLocation": (BRICK.hasLocation, BRICK.isLocationOf),
    "controls": (BRICK.controls, BRICK.isControlledBy),
    "isHostedBy": (BRICK.isHostedBy, BRICK.hosts),
}


def _device_uri(device_id: int, project_id: Optional[int]) -> URIRef:
    if project_id is not None:
        return URIRef(f"urn:iotistica:project:{project_id}:device:{device_id}")
    return URIRef(f"urn:iotistica:sim:device:{device_id}")


def _object_uri(object_id: int, project_id: Optional[int]) -> URIRef:
    if project_id is not None:
        return URIRef(f"urn:iotistica:project:{project_id}:object:{object_id}")
    return URIRef(f"urn:iotistica:sim:object:{object_id}")


def _location_uri(location_id: int, project_id: Optional[int]) -> URIRef:
    if project_id is not None:
        return URIRef(f"urn:iotistica:project:{project_id}:location:{location_id}")
    return URIRef(f"urn:iotistica:sim:location:{location_id}")


def _equipment_uri(equipment_id: int, project_id: Optional[int]) -> URIRef:
    if project_id is not None:
        return URIRef(f"urn:iotistica:project:{project_id}:equipment:{equipment_id}")
    return URIRef(f"urn:iotistica:sim:equipment:{equipment_id}")


def _entity_uri(entity_id: int, project_id: Optional[int]) -> URIRef:
    """Last-resort URI for a semantic entity that isn't backed by an
    existing device/object/location row -- sub-equipment (Supply_Fan/
    Return_Fan) and virtual, device-hosted locations (Lighting_Zone)."""
    if project_id is not None:
        return URIRef(f"urn:iotistica:project:{project_id}:entity:{entity_id}")
    return URIRef(f"urn:iotistica:sim:entity:{entity_id}")


def build_brick_graph(
    devices: list[dict],
    project_id: Optional[int] = None,
    entities: Optional[list[dict]] = None,
    relationships: Optional[list[dict]] = None,
) -> tuple[Graph, list[str]]:
    """
    Builds the shared semantic model for every device (each with an
    "objects" list, same shape EDE export/project data already uses).
    Returns (graph, warnings) — warnings covers requirement 8 (a point
    missing a Brick class, or a device missing an equipment class) without
    aborting the export; everything that *can* be exported still is.

    entities/relationships (semantic_entities/semantic_relationships rows,
    Brick Core -- optional, default-empty, zero behavior change for
    existing callers that don't pass them) add isPartOf/feeds/hasLocation
    triples on top of the per-device/per-object hasPoint/isPointOf this
    function already emits unconditionally. See _resolve_entity_uri()
    below for how each entity's node is chosen.
    """
    graph = Graph()
    graph.bind("brick", BRICK)
    graph.bind("ref", REF)
    graph.bind("bacnet", BACNET)
    graph.bind("unit", QUDT_UNIT)

    warnings: list[str] = []

    for dev in devices:
        device_id = dev["id"]
        device_name = dev.get("name", f"#{device_id}")
        device_uri = _device_uri(device_id, project_id)

        graph.add((device_uri, RDF.type, BACNET.BACnetDevice))
        graph.add((device_uri, RDFS.label, Literal(device_name)))
        graph.add((device_uri, BACNET["device-instance"], Literal(dev["device_instance"], datatype=XSD.integer)))

        equipment_type = dev.get("equipment_type")
        if equipment_type and equipment_type in EQUIPMENT_TYPES:
            graph.add((device_uri, RDF.type, BRICK[equipment_type]))
        else:
            graph.add((device_uri, RDF.type, BRICK.Equipment))
            warnings.append(f"{device_name}: no equipment_type set, exported as generic brick:Equipment")

        for obj in dev.get("objects", []):
            object_id = obj["id"]
            object_name = obj.get("name", f"#{object_id}")
            object_uri = _object_uri(object_id, project_id)

            graph.add((object_uri, RDFS.label, Literal(object_name)))

            point_type = obj.get("point_type")
            if point_type and point_type in POINT_TYPES:
                graph.add((object_uri, RDF.type, BRICK[point_type]))
            else:
                # Fall back to the real Brick root class rather than
                # leaving the node untyped, so a consumer that doesn't
                # already know about isPointOf can still recognize this as
                # a point — "it is a point, but its precise semantic type
                # is unknown" — while still recording the warning.
                graph.add((object_uri, RDF.type, BRICK.Point))
                warnings.append(f"{device_name} / {object_name}: no point_type set, exported as generic brick:Point")

            qudt_unit = UNIT_TO_QUDT.get(obj.get("units", ""))
            if qudt_unit:
                graph.add((object_uri, BRICK.hasUnit, QUDT_UNIT[qudt_unit]))

            # Equipment <-> Point (requirement 5). Equipment-to-equipment
            # relationships (feeds/hasPart/isPartOf) are real Brick
            # predicates but there's no such relationship data in this
            # simulator today — nothing to export, so nothing is emitted.
            graph.add((device_uri, BRICK.hasPoint, object_uri))
            graph.add((object_uri, BRICK.isPointOf, device_uri))

            # BACnet metadata (requirement 6), following the real Brick
            # ref-extension pattern verbatim: a blank ref:BACnetReference
            # node carrying the object-identifier ("<type>,<instance>",
            # matching BrickSchema/Brick's own example format exactly),
            # object-name, and a link back to the owning device. Device
            # Instance already lives on the device node itself above.
            reference = BNode()
            graph.add((object_uri, REF.hasExternalReference, reference))
            graph.add((reference, RDF.type, REF.BACnetReference))
            graph.add((reference, BACNET["object-identifier"], Literal(f"{obj['object_type']},{obj['object_instance']}")))
            graph.add((reference, BACNET["object-name"], Literal(object_name)))
            graph.add((reference, BACNET.objectOf, device_uri))

    entities = entities or []
    relationships = relationships or []

    # Sub-equipment (e.g. a Supply_Fan) is any entity that is itself the
    # SOURCE of an isPartOf edge -- distinguishes it from a device's own
    # top-level equipment entity, which reuses the device's URI instead of
    # minting a new one (see _resolve_entity_uri).
    sub_equipment_ids = {
        rel["source_entity_id"] for rel in relationships if rel["predicate"] == "isPartOf"
    }

    entity_uri_by_id: dict[int, URIRef] = {}
    for ent in entities:
        uri = _resolve_entity_uri(ent, project_id, sub_equipment_ids)
        entity_uri_by_id[ent["id"]] = uri

        graph.add((uri, RDFS.label, Literal(ent["name"])))

        brick_class = ent["brick_class"]
        if (
            brick_class in EQUIPMENT_TYPES
            or brick_class in POINT_TYPES
            or brick_class in LOCATION_KINDS
            or brick_class in CONTROLLER_TYPES
        ):
            graph.add((uri, RDF.type, BRICK[brick_class]))
        else:
            warnings.append(f"semantic entity {ent['name']!r}: brick_class {brick_class!r} is not canonical, skipping RDF.type")

    for rel in relationships:
        pair = PREDICATE_TO_BRICK.get(rel["predicate"])
        if pair is None:
            continue  # defensive -- the DB CHECK constraint already rejects unknown predicates

        source_uri = entity_uri_by_id.get(rel["source_entity_id"])
        target_uri = entity_uri_by_id.get(rel["target_entity_id"])
        if source_uri is None or target_uri is None:
            # Endpoint entity wasn't included in `entities` (out of this
            # export's scope, e.g. a device-scoped export where the other
            # endpoint belongs to a different device) -- skip the triple
            # rather than fail the whole export.
            continue

        forward, inverse = pair
        graph.add((source_uri, forward, target_uri))
        graph.add((target_uri, inverse, source_uri))

    _add_controller_hosting_triples(graph, devices, entities, project_id)

    return graph, warnings


def _add_controller_hosting_triples(
    graph: Graph,
    devices: list[dict],
    entities: list[dict],
    project_id: Optional[int],
) -> None:
    """Derived (never persisted) Controller -> Point hosting: one
    isHostedBy/hosts triple pair per BACnet object owned by an explicitly-
    marked Controller device. Same objects.device_id join the Semantic
    Graph UI performs client-side at render time (see admin/src/components/
    SemanticPanel.vue's derivedHostingEdges) -- kept here as the backend's
    single source of truth for this derivation so graph and export can't
    disagree, even though the two can't literally share code across the
    Python/TypeScript boundary.

    Gated on an actual entity_kind='controller' semantic entity existing
    for that device -- never inferred or bulk-applied; a device merely
    having objects doesn't imply it has been explicitly marked a Controller
    (see sync_controller_entity()'s docstring in src/semantics/mirror.py).

    Reuses the device's own URI (see _resolve_entity_uri's controller
    branch: Controller is a semantic ROLE of the device, not a separate
    node) and covers every object regardless of point_type classification
    -- every object already got an object_uri + RDF.type triple in the
    per-device loop above (classified brick_class or generic brick:Point),
    matching the Semantic Graph UI's own "unclassified points still count
    as hosted" behavior.
    """
    controller_device_ids = {
        ent["device_id"] for ent in entities
        if ent["entity_kind"] == "controller" and ent.get("device_id") is not None
    }
    for dev in devices:
        if dev["id"] not in controller_device_ids:
            continue
        controller_uri = _device_uri(dev["id"], project_id)
        for obj in dev.get("objects", []):
            point_uri = _object_uri(obj["id"], project_id)
            graph.add((point_uri, BRICK.isHostedBy, controller_uri))
            graph.add((controller_uri, BRICK.hosts, point_uri))


def _resolve_entity_uri(
    entity: dict,
    project_id: Optional[int],
    sub_equipment_ids: set[int],
) -> URIRef:
    """Reuses the same URI the per-device/per-object loop above already
    minted wherever the entity is backed by an existing device/object/
    location row, so new Brick Core triples land on the SAME node rather
    than a disconnected duplicate:
      - point entity with object_id set -> the object's URI.
      - location entity with location_id set -> the location's URI (a
        real row in `locations`, part of the site/building/floor/room
        hierarchy).
      - equipment entity with device_id set, that is NOT itself
        sub-equipment (not a source of an isPartOf edge) -> the device's
        URI -- this is "the device's own equipment", already typed by the
        per-device loop.
      - equipment entity with equipment_id set -> a dedicated equipment
        URI (there's no per-equipment-row loop like the per-device one
        above, so this mints -- deterministically, from the stable
        `equipment` row id -- rather than falling through to the generic
        entity-id URI below).
      - controller entity (always has device_id set, enforced by
        validate_semantic_entity) -> the device's URI -- Controller is a
        semantic ROLE of an existing device, not a separate node; reusing
        the device URI is what makes `brick:Controller` land on the same
        node already typed bacnet:BACnetDevice by the per-device loop.
    Everything else -- sub-equipment (Supply_Fan/Return_Fan) and virtual,
    device-hosted locations (Lighting_Zone with location_id IS NULL) --
    mints a fresh entity URI, since there's no existing device/object/
    location row that represents it on its own.
    """
    entity_kind = entity["entity_kind"]

    if entity_kind == "point" and entity.get("object_id") is not None:
        return _object_uri(entity["object_id"], project_id)

    if entity_kind == "location" and entity.get("location_id") is not None:
        return _location_uri(entity["location_id"], project_id)

    if entity_kind == "equipment" and entity.get("equipment_id") is not None:
        return _equipment_uri(entity["equipment_id"], project_id)

    if (
        entity_kind == "equipment"
        and entity.get("device_id") is not None
        and entity["id"] not in sub_equipment_ids
    ):
        return _device_uri(entity["device_id"], project_id)

    if entity_kind == "controller" and entity.get("device_id") is not None:
        return _device_uri(entity["device_id"], project_id)

    return _entity_uri(entity["id"], project_id)


def graph_to_ttl(graph: Graph, warnings: list[str]) -> str:
    """The only format-specific function — a future graph_to_jsonld/
    graph_to_rdfxml would just call graph.serialize(format=...) on the
    same graph build_brick_graph() already produced."""
    ttl = graph.serialize(format="turtle")
    if not warnings:
        return ttl
    header = "\n".join(f"# WARNING: {w}" for w in warnings)
    return f"{header}\n\n{ttl}"

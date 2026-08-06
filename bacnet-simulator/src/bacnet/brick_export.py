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

from ..core.config import EQUIPMENT_TYPES, POINT_TYPES

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


def _device_uri(device_id: int, project_id: Optional[int]) -> URIRef:
    if project_id is not None:
        return URIRef(f"urn:iotistica:project:{project_id}:device:{device_id}")
    return URIRef(f"urn:iotistica:sim:device:{device_id}")


def _object_uri(object_id: int, project_id: Optional[int]) -> URIRef:
    if project_id is not None:
        return URIRef(f"urn:iotistica:project:{project_id}:object:{object_id}")
    return URIRef(f"urn:iotistica:sim:object:{object_id}")


def build_brick_graph(devices: list[dict], project_id: Optional[int] = None) -> tuple[Graph, list[str]]:
    """
    Builds the shared semantic model for every device (each with an
    "objects" list, same shape EDE export/project data already uses).
    Returns (graph, warnings) — warnings covers requirement 8 (a point
    missing a Brick class, or a device missing an equipment class) without
    aborting the export; everything that *can* be exported still is.
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

    return graph, warnings


def graph_to_ttl(graph: Graph, warnings: list[str]) -> str:
    """The only format-specific function — a future graph_to_jsonld/
    graph_to_rdfxml would just call graph.serialize(format=...) on the
    same graph build_brick_graph() already produced."""
    ttl = graph.serialize(format="turtle")
    if not warnings:
        return ttl
    header = "\n".join(f"# WARNING: {w}" for w in warnings)
    return f"{header}\n\n{ttl}"

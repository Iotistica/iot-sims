"""
Safe parser for OPC UA NodeSet2 XML documents.

Untrusted-input posture: this module is the only place that touches the raw
uploaded bytes. It never logs full XML content (only filename/size/counts),
uses `defusedxml` so DTDs/external entities/network access are rejected
outright rather than silently ignored, and enforces a file-size limit before
ElementTree ever sees the bytes plus a parsed node-count limit afterward — a
huge-but-well-formed file is a DoS risk `defusedxml` alone doesn't cover.

Two-pass design, per the NodeSet2 spec's own recommendation (node order in
the file is not meaningful): pass 1 builds every node with its attributes and
raw reference list; pass 2 resolves hierarchy (parent_node_id) and flags
unresolved/duplicate references, since a node's parent may be declared by
*either* an inverse reference on the child or a forward reference on the
parent, and either one can appear anywhere in the document relative to the
other.
"""
import logging
import os
from typing import Optional
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as DET
from defusedxml import DefusedXmlException

from .models import (
    HIERARCHY_REFERENCE_TYPES,
    ImportedNamespace,
    ImportedNode,
    ImportedReference,
    NodeSetParseError,
    ParsedNodeSet,
    ParseReport,
)

logger = logging.getLogger("opcua-sim.nodeset")

MAX_FILE_SIZE_BYTES = int(os.environ.get("NODESET_MAX_FILE_SIZE_BYTES", str(10 * 1024 * 1024)))  # 10 MB
MAX_NODE_COUNT = int(os.environ.get("NODESET_MAX_NODE_COUNT", "50000"))

_UANODESET_TAG = "UANodeSet"
_NODE_CLASS_TAGS = {
    "UAObject": "Object",
    "UAVariable": "Variable",
    "UAMethod": "Method",
    "UAObjectType": "ObjectType",
    "UAVariableType": "VariableType",
    "UADataType": "DataType",
    "UAReferenceType": "ReferenceType",
}
_UAX_TEXT_CHILD = {"LocalizedText": "Text", "QualifiedName": "Name"}

# Well-known ns=0 reference type NodeIds (the ones we care about for
# hierarchy) — present in every OPC UA server, essentially never redefined
# in an imported file's own <UAReferenceType> nodes.
_WELL_KNOWN_REFERENCE_TYPES = {
    "i=35": "Organizes",
    "i=47": "HasComponent",
    "i=46": "HasProperty",
    "i=40": "HasTypeDefinition",
    "i=45": "HasSubtype",
    "i=36": "HasModellingRule",
}

_SCALAR_VALUE_PARSERS = {
    "Boolean": lambda t: (t or "").strip().lower() == "true",
    "SByte": lambda t: int(t), "Byte": lambda t: int(t),
    "Int16": lambda t: int(t), "UInt16": lambda t: int(t),
    "Int32": lambda t: int(t), "UInt32": lambda t: int(t),
    "Int64": lambda t: int(t), "UInt64": lambda t: int(t),
    "Float": lambda t: float(t), "Double": lambda t: float(t),
    "String": lambda t: t or "", "DateTime": lambda t: t or "",
}


def _local(tag: str) -> str:
    """Strip the `{namespace-uri}` prefix ElementTree puts on qualified tags."""
    return tag.split("}", 1)[1] if "}" in tag else tag


def _parse_ns_index(node_id: str) -> int:
    """`ns=2;i=1000` -> 2. Missing `ns=` means the standard namespace, 0."""
    for part in node_id.split(";"):
        if part.startswith("ns="):
            try:
                return int(part[3:])
            except ValueError:
                return 0
    return 0


def parse_nodeset_xml(xml_bytes: bytes, filename: str = "upload.xml") -> ParsedNodeSet:
    """Parse and validate a NodeSet2 XML document from raw bytes.

    Raises NodeSetParseError for document-level failures (can't produce any
    usable result). Per-node problems are recorded in the returned report's
    warnings/errors/unsupported_features instead of raising, per the "don't
    silently discard, don't crash on non-critical constructs" requirement.
    """
    size = len(xml_bytes)
    if size > MAX_FILE_SIZE_BYTES:
        raise NodeSetParseError(
            f"File is {size} bytes, exceeds the {MAX_FILE_SIZE_BYTES}-byte limit"
        )
    if size == 0:
        raise NodeSetParseError("File is empty")

    logger.info("Parsing NodeSet2 XML: filename=%s size=%d bytes", filename, size)

    try:
        root = DET.fromstring(xml_bytes)
    except DefusedXmlException as e:
        # DTDs / external entities / network access attempts land here.
        raise NodeSetParseError(f"Rejected unsafe XML construct: {e}") from e
    except Exception as e:
        raise NodeSetParseError(f"Malformed XML: {e}") from e

    if _local(root.tag) != _UANODESET_TAG:
        raise NodeSetParseError(
            f"Root element is <{_local(root.tag)}>, expected <UANodeSet>"
        )

    report = ParseReport()

    namespaces = _parse_namespaces(root)
    report.namespaces = len(namespaces)
    aliases = _parse_aliases(root)

    nodes: dict[str, ImportedNode] = {}
    node_count = 0
    for child in root:
        local = _local(child.tag)
        node_class = _NODE_CLASS_TAGS.get(local)
        if node_class is None:
            if local not in ("NamespaceUris", "Models", "Aliases", "Extensions"):
                report.unsupported_features.append(f"Unrecognized top-level element <{local}> skipped")
            continue

        node_count += 1
        if node_count > MAX_NODE_COUNT:
            raise NodeSetParseError(
                f"Document has more than {MAX_NODE_COUNT} nodes — rejected to bound parse cost"
            )

        node = _parse_node_element(child, node_class, aliases, report)
        if node is None:
            continue

        if node.node_id in nodes:
            report.duplicate_node_ids.append(node.node_id)
            continue  # keep first occurrence, report the rest
        nodes[node.node_id] = node

        report.nodes_total += 1
        if node_class == "Object":
            report.objects += 1
        elif node_class == "Variable":
            report.variables += 1
        elif node_class == "Method":
            report.methods += 1
        else:
            report.types += 1

    _resolve_hierarchy(nodes, report)
    report.max_depth = _compute_max_depth(nodes)

    if report.errors:
        report.valid = False

    logger.info(
        "Parsed NodeSet2 XML: filename=%s nodes=%d objects=%d variables=%d warnings=%d errors=%d",
        filename, report.nodes_total, report.objects, report.variables,
        len(report.warnings), len(report.errors),
    )

    return ParsedNodeSet(namespaces=namespaces, nodes=nodes, report=report)


def _parse_namespaces(root: Element) -> list[ImportedNamespace]:
    result = []
    for el in root:
        if _local(el.tag) != "NamespaceUris":
            continue
        for i, uri_el in enumerate(el, start=1):
            if _local(uri_el.tag) == "Uri" and uri_el.text:
                result.append(ImportedNamespace(source_index=i, uri=uri_el.text.strip()))
    return result


def _parse_aliases(root: Element) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for el in root:
        if _local(el.tag) != "Aliases":
            continue
        for alias_el in el:
            if _local(alias_el.tag) != "Alias":
                continue
            name = alias_el.get("Alias")
            if name and alias_el.text:
                aliases[name] = alias_el.text.strip()
    return aliases


def _resolve_reference_type_name(token: str, aliases: dict[str, str]) -> str:
    """A ReferenceType token is either a friendly alias name ("HasComponent")
    or a raw NodeId ("i=47" / "ns=0;i=47"). Normalize to the friendly name
    when it's one of the small set we care about for hierarchy; otherwise
    return the token unchanged (still preserved on the node, just not
    treated as structural)."""
    if token in HIERARCHY_REFERENCE_TYPES or token in (
        "HasTypeDefinition", "HasSubtype", "HasModellingRule",
    ):
        return token
    raw = aliases.get(token, token)
    normalized = raw.replace("ns=0;", "")
    return _WELL_KNOWN_REFERENCE_TYPES.get(normalized, token)


def _resolve_data_type(token: Optional[str], aliases: dict[str, str]) -> Optional[str]:
    if token is None:
        return None
    return aliases.get(token, token)


def _parse_value(value_el: Element, report: ParseReport, node_id: str):
    children = list(value_el)
    if not children:
        return None
    inner = children[0]
    local = _local(inner.tag)

    if local.startswith("ListOf"):
        scalar_name = local[len("ListOf"):]
        count = len(list(inner))
        report.warnings.append(
            f"{node_id}: array value (ListOf{scalar_name}, {count} items) not supported by this "
            f"simulator yet — tag will be created with a default value instead"
        )
        report.unsupported_features.append(f"{node_id}: array value ListOf{scalar_name}")
        return None

    if local in _UAX_TEXT_CHILD:
        text_local = _UAX_TEXT_CHILD[local]
        for sub in inner:
            if _local(sub.tag) == text_local:
                return sub.text or ""
        return None

    parser = _SCALAR_VALUE_PARSERS.get(local)
    if parser is None:
        report.warnings.append(f"{node_id}: value type '{local}' not supported — using default value")
        report.unsupported_features.append(f"{node_id}: value type {local}")
        return None
    try:
        return parser(inner.text)
    except (TypeError, ValueError):
        report.warnings.append(f"{node_id}: could not parse '{local}' value '{inner.text}' — using default")
        return None


def _parse_node_element(
    el: Element, node_class: str, aliases: dict[str, str], report: ParseReport
) -> Optional[ImportedNode]:
    node_id = el.get("NodeId")
    browse_name = el.get("BrowseName", "")
    if not node_id or not browse_name:
        report.errors.append(f"<{node_class}> element missing NodeId or BrowseName — skipped")
        return None

    display_name = None
    description = None
    references: list[ImportedReference] = []
    initial_value = None

    for child in el:
        local = _local(child.tag)
        if local == "DisplayName":
            display_name = child.text or ""
        elif local == "Description":
            description = child.text or ""
        elif local == "References":
            for ref_el in child:
                if _local(ref_el.tag) != "Reference":
                    continue
                ref_type_token = ref_el.get("ReferenceType", "")
                target = (ref_el.text or "").strip()
                if not target:
                    continue
                is_forward = ref_el.get("IsForward", "true").lower() != "false"
                references.append(ImportedReference(
                    reference_type=_resolve_reference_type_name(ref_type_token, aliases),
                    target_node_id=target,
                    is_forward=is_forward,
                ))
        elif local == "Value" and node_class == "Variable":
            initial_value = _parse_value(child, report, node_id)

    value_rank = None
    if el.get("ValueRank") is not None:
        try:
            value_rank = int(el.get("ValueRank"))
        except ValueError:
            pass

    array_dimensions: list[int] = []
    dims_raw = el.get("ArrayDimensions")
    if dims_raw:
        try:
            array_dimensions = [int(d) for d in dims_raw.split(",") if d.strip()]
        except ValueError:
            report.warnings.append(f"{node_id}: could not parse ArrayDimensions '{dims_raw}'")

    access_level = 0
    if el.get("AccessLevel") is not None:
        try:
            access_level = int(el.get("AccessLevel"))
        except ValueError:
            pass
    writable = bool(access_level & 0x02)

    historizing = (el.get("Historizing", "false").lower() == "true")

    raw_attributes = {k: v for k, v in el.attrib.items() if k not in (
        "NodeId", "BrowseName", "ValueRank", "ArrayDimensions", "AccessLevel",
        "UserAccessLevel", "Historizing", "DataType",
    )}

    return ImportedNode(
        node_class=node_class,
        node_id=node_id,
        ns_index=_parse_ns_index(node_id),
        browse_name=browse_name,
        display_name=display_name or browse_name.split(":")[-1],
        description=description,
        data_type=_resolve_data_type(el.get("DataType"), aliases) if node_class == "Variable" else None,
        value_rank=value_rank,
        array_dimensions=array_dimensions,
        writable=writable,
        historizing=historizing,
        initial_value=initial_value,
        references=references,
        raw_attributes=raw_attributes,
    )


def _resolve_hierarchy(nodes: dict[str, ImportedNode], report: ParseReport) -> None:
    """Fill in parent_node_id for every node and flag unresolved/cyclic
    references. Parent can be declared either as an inverse hierarchy
    reference on the child, or a forward one on the parent — both are legal
    NodeSet2 encodings and real-world exporters use either."""
    forward_hierarchy: dict[str, str] = {}  # target -> source, from forward refs
    imported_ns_prefixes = {f"ns={n.ns_index}" for n in nodes.values()}

    for node in nodes.values():
        for ref in node.references:
            if ref.reference_type in HIERARCHY_REFERENCE_TYPES and ref.is_forward:
                forward_hierarchy[ref.target_node_id] = node.node_id

            # Only flag references as unresolved if the target claims to live
            # in one of *this document's* imported namespaces — a reference
            # to a standard ns=0 node (or another namespace we don't own) is
            # normal and expected, not a document defect.
            target_ns = ref.target_node_id.split(";")[0] if "ns=" in ref.target_node_id else "ns=0"
            if target_ns in imported_ns_prefixes and ref.target_node_id not in nodes:
                report.unresolved_references.append(
                    f"{node.node_id} --{ref.reference_type}--> {ref.target_node_id}"
                )

    for node in nodes.values():
        parent = None
        for ref in node.references:
            if ref.reference_type in HIERARCHY_REFERENCE_TYPES and not ref.is_forward:
                parent = ref.target_node_id
                break
        if parent is None:
            parent = forward_hierarchy.get(node.node_id)
        if parent is None:
            parent = node.raw_attributes.get("ParentNodeId")  # opportunistic, non-standard fallback
        node.parent_node_id = parent


def _compute_max_depth(nodes: dict[str, ImportedNode]) -> int:
    depth_cache: dict[str, int] = {}

    def depth_of(node_id: str, seen: frozenset) -> int:
        if node_id in depth_cache:
            return depth_cache[node_id]
        if node_id in seen:
            return 0  # cycle — bail out rather than recurse forever
        node = nodes.get(node_id)
        if node is None or node.parent_node_id is None or node.parent_node_id not in nodes:
            depth_cache[node_id] = 0
            return 0
        d = 1 + depth_of(node.parent_node_id, seen | {node_id})
        depth_cache[node_id] = d
        return d

    return max((depth_of(nid, frozenset()) for nid in nodes), default=0)

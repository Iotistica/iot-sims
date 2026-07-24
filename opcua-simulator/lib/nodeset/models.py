"""
Internal representation of a parsed OPC UA NodeSet2 XML document.

Deliberately separate from the simulator's `devices`/`tags` DB schema (see
lib/db.py) — these dataclasses model the *source* document as-is (its own
NodeIds, namespace indices, hierarchy) before any adapter decides how much of
it maps onto a device/tag row. Keeping that boundary means the parser never
needs to know anything about the simulator's schema, and the schema never
needs to know anything about NodeSet2 XML.

Scope note (see docs/nodeset-import.md): this first pass only carries enough
fields to support Objects and Variables organized in a hierarchy. Method/
ObjectType/VariableType/DataType/ReferenceType nodes are counted and reported
but not deeply modeled — `raw_attributes` is where anything not promoted to a
named field ends up, so nothing is silently discarded even when it isn't
acted on yet.
"""
from dataclasses import dataclass, field
from typing import Any, Optional

# Node classes this parser understands structurally. Anything else parses far
# enough to be counted and reported, never crashes the import.
STRUCTURAL_NODE_CLASSES = {"Object", "Variable"}
COUNTED_NODE_CLASSES = {
    "Object", "Variable", "Method", "ObjectType", "VariableType", "DataType", "ReferenceType",
}

# Reference types that define parent/child hierarchy for the adapter's
# device/tag flattening. Everything else is preserved on the node's
# `references` list but doesn't affect placement.
HIERARCHY_REFERENCE_TYPES = {"Organizes", "HasComponent", "HasProperty"}


@dataclass
class ImportedNamespace:
    source_index: int
    uri: str
    runtime_index: Optional[int] = None


@dataclass
class ImportedReference:
    reference_type: str
    target_node_id: str
    is_forward: bool


@dataclass
class ImportedNode:
    node_class: str
    node_id: str                      # raw source NodeId string, e.g. "ns=1;s=Machine1.Temperature"
    ns_index: int
    browse_name: str
    display_name: str
    description: Optional[str] = None
    data_type: Optional[str] = None   # resolved (alias-expanded) NodeId string of the DataType node
    value_rank: Optional[int] = None
    array_dimensions: list[int] = field(default_factory=list)
    writable: bool = False
    historizing: bool = False
    initial_value: Any = None
    parent_node_id: Optional[str] = None
    references: list[ImportedReference] = field(default_factory=list)
    raw_attributes: dict = field(default_factory=dict)


@dataclass
class ParseIssue:
    message: str
    node_id: Optional[str] = None


@dataclass
class ParseReport:
    valid: bool = True
    namespaces: int = 0
    nodes_total: int = 0
    objects: int = 0
    variables: int = 0
    methods: int = 0
    types: int = 0
    max_depth: int = 0
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unresolved_references: list[str] = field(default_factory=list)
    unsupported_features: list[str] = field(default_factory=list)
    duplicate_node_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "valid": self.valid,
            "namespaces": self.namespaces,
            "nodes_total": self.nodes_total,
            "objects": self.objects,
            "variables": self.variables,
            "methods": self.methods,
            "types": self.types,
            "max_depth": self.max_depth,
            "warnings": self.warnings,
            "errors": self.errors,
            "unresolved_references": self.unresolved_references,
            "unsupported_features": self.unsupported_features,
            "duplicate_node_ids": self.duplicate_node_ids,
        }


@dataclass
class ParsedNodeSet:
    """Full result of parsing one NodeSet2 XML document."""
    namespaces: list[ImportedNamespace]
    nodes: dict[str, ImportedNode]   # keyed by raw source NodeId string
    report: ParseReport


class NodeSetParseError(Exception):
    """Raised for document-level failures (malformed XML, wrong root, oversized, etc.) —
    anything that means there is no usable ParsedNodeSet at all, as opposed to
    a per-node issue that gets recorded in ParseReport.warnings/errors instead."""

"""Brick-portable feature identity for classifier models (condition/fault_type
RandomForestClassifier training, see training_export.py's build_classification_dataset).

Three feature identity kinds, chosen per selected training point:
  - "brick": auto-resolved from the point's own Brick classification (semantic_entities) to a
    (point_class, equipment_path) pair -- genuinely portable, resolvable against ANY device with
    equivalent Brick classification, not just the one it was trained on.
  - "custom": the point has no Brick classification; the user explicitly typed a human-meaningful
    label for it rather than accepting a raw id. Same-device-only in this phase (see the plan's
    Scope boundary -- a cross-device custom-label vocabulary isn't built yet).
  - "device_specific": the user explicitly accepted this feature is tied to this exact
    source_object_id, no semantic meaning at all -- exactly today's (pre-semantic) behavior, kept
    as an honest, clearly-labeled choice. Same-device-only.

Critical invariant this whole module exists to protect: a "brick" resolution is always attempted
first and automatically; "custom"/"device_specific" are NEVER chosen automatically as a fallback
when Brick resolution fails -- that failure is reported as `undecided` and the caller (the
training-job route) must have an explicit, caller-supplied choice for it, or the job is rejected.
Missing Brick coverage blocks nothing by itself; missing a DECISION does.

Built entirely from existing primitives (src/semantics/mirror.py, Database.get_related_entities/
get_semantic_entities) -- no new schema, no new tables. See src/semantics/resolver.py's
SemanticResolver for the established (but narrower/hardcoded) precedent this generalizes:
get_equipment_entity's "not itself the source of an isPartOf edge" top-level check, and the same
isPartOf/isPointOf edge directions, are reused here, not reinvented.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

FeatureIdentityKind = str  # "brick" | "custom" | "device_specific" -- plain str, not an enum, to
# match this codebase's own convention for small fixed vocabularies stored as TEXT (e.g.
# recordings.condition) rather than introducing a new Enum type for a 3-value set.

_IDENTITY_KINDS = ("brick", "custom", "device_specific")
# Least-portable-identity-kind-wins ordering for portability computation -- see
# compute_portability(). Note this maps an *identity kind* rank to a *portability status* label --
# the two are separate vocabularies that happen to share one name ("device_specific") by
# coincidence: "custom" identity kind maps to "partial" portability, not to itself.
_PORTABILITY_RANK = {"device_specific": 0, "custom": 1, "brick": 2}
_PORTABILITY_LABEL_BY_RANK = {0: "device_specific", 1: "partial", 2: "portable"}


@dataclass(frozen=True)
class ResolvedBrickPoint:
    """One object_id's resolved Brick identity -- point_class is the point's own semantic_entities
    brick_class; equipment_path is the isPartOf ancestor chain from the point's immediate owning
    equipment up to (excluding) the device's top-level equipment entity, outer-to-inner ([] for a
    point owned directly by the top-level equipment)."""
    point_class: str
    equipment_path: tuple[str, ...]
    unit: Optional[str] = None
    # True when this identity came from a live graph walk (resolve_point_semantic_identity),
    # i.e. equipment_path is authoritative. False when it was reconstructed from a flat
    # class snapshot with no equipment context (a deleted/unclassified recording point's
    # point_type mirror) -- see brick_adapter's candidate matcher.
    source_verified: bool = False


def resolve_point_semantic_identity(database: Any, object_id: int) -> Optional[ResolvedBrickPoint]:
    """object_id -> its Brick identity, or None if the point has no semantic_entities row at all
    (unclassified) or is classified but not linked to any owning equipment (a point entity with no
    isPointOf edge -- classified in isolation, not yet related; treated the same as unresolvable,
    never guessed at).

    Walk: find_point_entity-equivalent (get_semantic_entities(entity_kind='point',
    object_id=...)) for the point's own brick_class, then isPointOf (direction='out', the point is
    always the SOURCE of this edge) for its immediate owner, then repeated isPartOf
    (direction='out', sub-equipment is always the SOURCE) until an entity with no isPartOf parent
    is reached -- that's the top-level equipment, excluded from equipment_path itself, matching
    SemanticResolver.get_equipment_entity's own "not itself the source of an isPartOf edge" check.
    Assumes a tree, not a DAG (takes the first parent at each level) -- matches every other
    consumer of this graph in this codebase (SemanticResolver, mirror.py); a real multi-parent
    isPartOf hasn't been observed/built for anywhere else either.
    """
    point_entities = database.get_semantic_entities(entity_kind="point", object_id=object_id)
    if not point_entities:
        return None
    point_entity = point_entities[0]

    owners = database.get_related_entities(point_entity["id"], "isPointOf", direction="out")
    if not owners:
        return None
    current = owners[0]

    chain: list[dict] = []
    while True:
        parents = database.get_related_entities(current["id"], "isPartOf", direction="out")
        if not parents:
            break
        chain.append(current)
        current = parents[0]
    equipment_path = tuple(c["brick_class"] for c in reversed(chain))

    obj = database.get_object(object_id)
    unit = obj.get("units") if obj else None

    return ResolvedBrickPoint(
        point_class=point_entity["brick_class"], equipment_path=equipment_path, unit=unit,
        source_verified=True,
    )


def _feature_name(kind: str, *, point_class: str = "", equipment_path: tuple[str, ...] = (),
                   custom_label: str = "", object_name: str = "") -> str:
    """Deterministic, human-facing label -- informational only, never part of identity comparison
    (two specs with the same point_class/equipment_path collide regardless of what their generated
    names happen to be)."""
    if kind == "brick":
        parts = [*equipment_path, point_class]
        return "_".join(p.lower() for p in parts)
    if kind == "custom":
        return custom_label.strip().lower().replace(" ", "_") or "custom_feature"
    return (object_name or "point").strip().lower().replace(" ", "_").replace("-", "_") or "device_point"


def _provenance(database: Any, object_id: int) -> dict[str, Any]:
    """bacnet_object_type/instance/name + source_device_id -- metadata only (see the plan: never
    read back for matching/binding), persisted purely so an old device_specific/custom model can
    be inspected/debugged/exported without a second lookup."""
    obj = database.get_object(object_id) or {}
    return {
        "source_device_id": obj.get("device_id"),
        "source_object_id": object_id,
        "bacnet_object_type": obj.get("object_type"),
        "bacnet_object_instance": obj.get("object_instance"),
        "object_name": obj.get("name"),
        "unit": obj.get("units"),
    }


@dataclass
class ContractResult:
    # variable_name -> semantic_features-shaped dict (identity_kind, point_class/equipment_path OR
    # custom_label OR provenance, unit, feature_name) for every variable that resolved to a
    # decision (brick automatically, or custom/device_specific via an explicit choice).
    specs: dict[str, dict] = field(default_factory=dict)
    # Variables where Brick resolution failed AND no explicit feature_choices entry was supplied --
    # this, not "unmapped to Brick", is what blocks training.
    undecided: list[str] = field(default_factory=list)
    # Pairs of variable names whose resolved brick (point_class, equipment_path) collide -- a
    # genuine ambiguity (two features claiming to be "the" same role), distinct from `undecided`.
    ambiguous_brick_pairs: list[tuple[str, str]] = field(default_factory=list)
    # Pairs of variable names whose custom_label collides.
    duplicate_custom_label_pairs: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.undecided and not self.ambiguous_brick_pairs and not self.duplicate_custom_label_pairs


def build_semantic_feature_contract(
    database: Any,
    mapping: dict[str, int],
    feature_choices: Optional[dict[str, dict]] = None,
) -> ContractResult:
    """mapping is {variable_name: source_object_id} -- the same shape training_export.py's
    build_classification_dataset already consumes (input-direction variables only). feature_choices
    is the caller's explicit decision for anything Brick can't resolve:
    {variable_name: {"kind": "custom", "custom_label": "..."} | {"kind": "device_specific"}} --
    entries for a variable that DID resolve via Brick are ignored (Brick always wins when it
    succeeds; a caller can't override a real Brick classification by supplying a choice).
    """
    feature_choices = feature_choices or {}
    result = ContractResult()

    brick_identity: dict[str, tuple[str, tuple[str, ...]]] = {}
    for variable_name, object_id in mapping.items():
        resolved = resolve_point_semantic_identity(database, object_id)
        if resolved is not None:
            result.specs[variable_name] = {
                "feature_name": _feature_name(
                    "brick", point_class=resolved.point_class, equipment_path=resolved.equipment_path,
                ),
                "identity_kind": "brick",
                "point_class": resolved.point_class,
                "equipment_path": list(resolved.equipment_path),
                "unit": resolved.unit,
            }
            brick_identity[variable_name] = (resolved.point_class, resolved.equipment_path)
            continue

        choice = feature_choices.get(variable_name)
        if choice is None or choice.get("kind") not in ("custom", "device_specific"):
            result.undecided.append(variable_name)
            continue

        provenance = _provenance(database, object_id)
        if choice["kind"] == "custom":
            custom_label = str(choice.get("custom_label") or "").strip()
            if not custom_label:
                result.undecided.append(variable_name)
                continue
            result.specs[variable_name] = {
                "feature_name": _feature_name("custom", custom_label=custom_label),
                "identity_kind": "custom",
                "custom_label": custom_label,
                **provenance,
            }
        else:
            result.specs[variable_name] = {
                "feature_name": _feature_name("device_specific", object_name=provenance.get("object_name") or ""),
                "identity_kind": "device_specific",
                **provenance,
            }

    # Ambiguity checks -- only meaningful among variables that actually resolved.
    seen_brick: dict[tuple[str, tuple[str, ...]], str] = {}
    for variable_name, identity in brick_identity.items():
        prior = seen_brick.get(identity)
        if prior is not None:
            result.ambiguous_brick_pairs.append((prior, variable_name))
        else:
            seen_brick[identity] = variable_name

    seen_labels: dict[str, str] = {}
    for variable_name, spec in result.specs.items():
        if spec["identity_kind"] != "custom":
            continue
        label = spec["custom_label"]
        prior = seen_labels.get(label)
        if prior is not None:
            result.duplicate_custom_label_pairs.append((prior, variable_name))
        else:
            seen_labels[label] = variable_name

    return result


def compute_portability(semantic_features: list[dict]) -> str:
    """Least-portable identity kind present wins -- one device_specific feature ties the whole
    model to its training device even if every other feature is brick-portable."""
    if not semantic_features:
        return "device_specific"
    worst = min(
        (_PORTABILITY_RANK.get(spec.get("identity_kind"), 0) for spec in semantic_features),
        default=0,
    )
    return _PORTABILITY_LABEL_BY_RANK[worst]


@dataclass
class BindingResult:
    # feature_name -> resolved object_id on the target device.
    bound: dict[str, int] = field(default_factory=dict)
    # feature_name -> reason string, for a requirement that couldn't be satisfied.
    missing: dict[str, str] = field(default_factory=dict)
    # feature_name -> the >=2 candidate object_ids that all matched, when a brick requirement can't
    # be disambiguated on the target device.
    ambiguous: dict[str, list[int]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.ambiguous


def resolve_semantic_contract_against_device(
    database: Any,
    device_id: int,
    semantic_features: list[dict],
    *,
    training_device_id: Optional[int] = None,
) -> BindingResult:
    """The reverse direction of build_semantic_feature_contract: given a model's persisted
    semantic_features and a TARGET device (may or may not be the device it was trained on),
    resolves each requirement to a concrete object_id on that device -- or reports why it
    couldn't.

    "brick" requirements are resolved by scanning the target device's own classified points for
    one whose resolved (point_class, equipment_path) matches -- this is what makes a `portable`
    model attachable to a different device. "custom"/"device_specific" requirements only ever
    resolve when device_id == training_device_id (a direct source_object_id existence check on
    that same device) -- attempting either on a different device is reported `missing` with a
    reason that says so explicitly, never silently skipped or guessed.
    """
    result = BindingResult()

    # Candidate points on the target device, pre-resolved once (not per-requirement) -- cheap
    # enough (one query + N per-object walks) for the object counts this simulator deals with, and
    # keeps the ambiguity check (multiple candidates matching one requirement) a simple grouping
    # rather than a second pass.
    target_objects = database.get_objects(device_id)
    resolved_by_identity: dict[tuple[str, tuple[str, ...]], list[int]] = {}
    for obj in target_objects:
        resolved = resolve_point_semantic_identity(database, obj["id"])
        if resolved is not None:
            resolved_by_identity.setdefault(
                (resolved.point_class, resolved.equipment_path), [],
            ).append(obj["id"])

    for spec in semantic_features:
        feature_name = spec["feature_name"]
        kind = spec["identity_kind"]

        if kind == "brick":
            identity = (spec["point_class"], tuple(spec.get("equipment_path") or ()))
            candidates = resolved_by_identity.get(identity, [])
            if not candidates:
                result.missing[feature_name] = (
                    f"No point classified as {spec['point_class']!r}"
                    + (f" under {'/'.join(spec['equipment_path'])}" if spec.get("equipment_path") else "")
                    + " was found on this equipment."
                )
            elif len(candidates) > 1:
                result.ambiguous[feature_name] = candidates
            else:
                result.bound[feature_name] = candidates[0]
            continue

        # custom / device_specific -- same-device-only in this phase.
        if training_device_id is not None and device_id != training_device_id:
            result.missing[feature_name] = (
                "This feature has no Brick classification, so it can only be used on the exact "
                "device this model was trained on -- not portable to different equipment."
            )
            continue
        object_id = spec.get("source_object_id")
        if object_id is None or database.get_object(object_id) is None:
            result.missing[feature_name] = "The original point for this feature no longer exists."
            continue
        result.bound[feature_name] = object_id

    return result


def _candidate_brick_identity(database: Any, candidate: Any) -> Optional[ResolvedBrickPoint]:
    """A ResolutionCandidate's Brick identity: a live graph walk when the source object is
    still classified (equipment_path authoritative, source_verified=True), else the flat
    ``brick.point_class`` snapshot captured at record time (class only, path unknown,
    source_verified=False), else None. Brick-internal helper for BrickSemanticProvider."""
    source_object_id = getattr(candidate, "source_object_id", None)
    if source_object_id is not None:
        walked = resolve_point_semantic_identity(database, source_object_id)
        if walked is not None:
            return walked
    snapshot = (getattr(candidate, "representation_snapshot", None) or {}).get("brick.point_class")
    if snapshot:
        return ResolvedBrickPoint(point_class=snapshot, equipment_path=(), unit=None, source_verified=False)
    return None


def match_brick_identity_in_candidates(
    database: Any,
    candidates: Any,
    target: ResolvedBrickPoint,
) -> dict[int, str]:
    """candidate.id -> "exact" | "class_only" | "unresolved" against ``target``.

    * "exact"      -- point_class matches AND (the target requires an equipment_path and
                      the candidate's verified path equals it) OR (the target requires no
                      path and the candidate identity was live-verified).
    * "class_only" -- point_class matches but the equipment context can't be confirmed.
    * "unresolved" -- point_class differs / candidate has no resolvable identity.

    Never guesses: a class-only candidate for a path-requiring target is "class_only",
    and the provider decides that is not enough to auto-bind.
    """
    out: dict[int, str] = {}
    for candidate in candidates:
        got = _candidate_brick_identity(database, candidate)
        if got is None or got.point_class != target.point_class:
            out[int(candidate.id)] = "unresolved"
            continue
        if target.equipment_path:
            out[int(candidate.id)] = "exact" if got.equipment_path == target.equipment_path else "class_only"
        else:
            out[int(candidate.id)] = "exact" if got.source_verified else "class_only"
    return out

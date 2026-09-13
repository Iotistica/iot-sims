"""Layer-2 generic semantic-resolution interface.

This module is provider-agnostic. It knows nothing about Brick, Haystack, OPC UA
information models, BACnet, or any protocol. It defines:

  * ``ResolutionCandidate`` -- a thing a model signal could bind to, in the caller's id
    space. Consumers do NOT construct these directly; they call a builder in
    ``src/semantics/candidates.py``.
  * ``SemanticResolution`` -- the neutral result. Consumers read ``candidate_id`` +
    ``quality`` (+ ``alternatives`` / ``per_candidate``); ``provenance`` is opaque
    UI/debug detail they must not branch on.
  * ``SemanticProvider`` -- the interface a representation (Brick today) implements.
  * ``resolve()`` -- applies the small set of genuinely provider-independent,
    capability-level rules, then delegates to the provider.

Consumers (calibration, Auto-Map) import from here + ``providers`` + ``candidates`` only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, Sequence

from .contract import SemanticContract

Quality = Literal["exact", "class_only", "ambiguous", "unresolved"]


@dataclass(frozen=True)
class ResolutionCandidate:
    """One binding target, in the CALLER's id space.

    - ``id``: returned verbatim as ``SemanticResolution.candidate_id`` (an ``objects.id``
      for Auto-Map, a ``recording_points.id`` for calibration).
    - ``source_object_id``: link to the live semantic graph if one exists (``None`` when
      the source was deleted or the candidate is synthetic). A provider MAY use it.
    - ``representation_snapshot``: provider-namespaced tags captured at record time, e.g.
      ``{"brick.point_class": "..."}``. Populated ONLY by ``candidates.py``. A provider
      reads only its own namespace; unknown namespaces are ignored.
    """

    id: int
    display_name: str = ""
    unit: str | None = None
    source_object_id: int | None = None
    representation_snapshot: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolutionContext:
    """Optional, neutral hints a provider/factory MAY use. None of it is required."""

    anchor_device_id: int | None = None
    # A dataset/device may name the semantic representation it was authored against.
    # None => use the project/global default provider.
    semantic_provider_hint: str | None = None


@dataclass(frozen=True)
class SemanticResolution:
    candidate_id: int | None
    quality: Quality
    provider: str
    provenance: dict[str, Any] = field(default_factory=dict)
    alternatives: tuple[int, ...] = ()
    per_candidate: dict[int, Quality] = field(default_factory=dict)


class SemanticProvider(Protocol):
    name: str

    def resolve(
        self,
        database: Any,
        contract: SemanticContract,
        candidates: Sequence[ResolutionCandidate],
        *,
        context: ResolutionContext | None = None,
    ) -> SemanticResolution: ...


def resolve(
    database: Any,
    provider: SemanticProvider,
    contract: SemanticContract | None,
    candidates: Sequence[ResolutionCandidate],
    *,
    context: ResolutionContext | None = None,
) -> SemanticResolution:
    """Apply the capability-level rules that hold for *every* provider, then delegate.

    The only genuinely provider-independent restrictions:

    * no contract at all -> ``"unresolved"``.
    * ``aggregation != "none"`` -> ``"unresolved"``. A *current resolver-capability
      limitation* -- ``SemanticResolution`` carries a single ``candidate_id`` -- NOT a
      permanent semantic rule. A provider must never satisfy an aggregated contract with
      one individual source point, so we stop before the provider is consulted.

    ``role == "derived"`` is deliberately NOT restricted here: it is a statement of
    meaning, and a provider may well have a representation for a calculated point.
    """
    name = getattr(provider, "name", "unknown")
    if contract is None or contract.is_empty:
        return SemanticResolution(None, "unresolved", name, {"reason": "no semantic contract"})
    if contract.is_aggregated:
        return SemanticResolution(
            None, "unresolved", name,
            {"reason": f"aggregation {contract.aggregation!r} not executed by the current resolver"},
        )
    return provider.resolve(database, contract, list(candidates), context=context)

"""Fires real OPC UA Alarm/Condition events from fault-behavior transitions,
so a real OPC UA client — not just this simulator's own dashboard — can see
them. Pairs with lib/analytics.py's record_fault_transition()/acknowledge_alarm()
(the dashboard-only sibling): both are driven from the same open/clear edges
in opcua_simulator.py's tick loop, kept as two separate call sites rather than
merged so a failure here can never affect the dashboard feed or vice versa.

v1 scope: fires a generic AlarmConditionType event with Severity/Message/
Retain/ActiveState/InputNode/NodeId set. No Acknowledge/Confirm method
handling (that part is genuinely not built — a client calling Acknowledge
has nowhere to land). ConditionRefresh, however, IS already implemented by
asyncua itself (server/subscription_service.py's condition_refresh(), keyed
off each event's NodeId field, which is why it's set here) — a client that
calls it gets every currently Retain=True condition server-wide, regardless
of which node its monitored item is on.

Known asyncua limitation (not something this module can work around):
event delivery only matches the *exact* node a client subscribed to
(monitored_item_service.py's trigger_event) — there's no hierarchical
propagation via HasNotifier references, even though such a reference is
browsable in the address space. To cover both common client patterns —
"watch this one specific device" and "watch the Server node for everything"
(the default behavior of node-opcua's installAlarmMonitoring(), which
iot-agent uses) — every alarm is fired TWICE: once with the device as the
emitting/delivery node, once with Server. SourceNode/InputNode still
correctly reference the real device/tag in both firings; only the
delivery-routing emitting_node differs.
"""
from __future__ import annotations

import logging
from typing import Any

from asyncua import Server, ua
from asyncua.common.event_objects import AlarmCondition
from asyncua.server.monitored_item_service import WhereClauseEvaluator

log = logging.getLogger("opcua-sim.alarms")

_HAS_SUBTYPE = ua.NodeId(ua.ObjectIds.HasSubtype)
_MAX_TYPE_WALK_DEPTH = 20  # generous — the real type hierarchy is nowhere near this deep


def _is_type_or_subtype(aspace: Any, type_id: ua.NodeId, target_id: ua.NodeId, _depth: int = 0) -> bool:
    """Walk inverse HasSubtype references from type_id up to its ancestors,
    returning True if type_id IS target_id or a descendant of it."""
    if type_id == target_id:
        return True
    if _depth >= _MAX_TYPE_WALK_DEPTH:
        return False
    node_data = aspace._nodes.get(type_id)
    if node_data is None:
        return False
    for ref in node_data.references:
        if ref.ReferenceTypeId == _HAS_SUBTYPE and not ref.IsForward:
            if _is_type_or_subtype(aspace, ref.NodeId, target_id, _depth + 1):
                return True
    return False


def patch_oftype_filter() -> None:
    """asyncua's WhereClauseEvaluator implements the spec's OfType filter
    operator as exact type equality (server/monitored_item_service.py) rather
    than "this type or any of its subtypes" — which is what OfType actually
    means per spec, and exactly what breaks node-opcua's
    installAlarmMonitoring() (it filters on AcknowledgeableConditionType, the
    parent of the AlarmConditionType events fired in this module, expecting
    the subtype to match). Confirmed via a real end-to-end test: without this
    patch, every alarm event is silently dropped server-side with "does not
    fit WhereClause, not generating event" and the client never sees it.

    Idempotent (guarded by a flag on the class) — safe to call multiple times
    or against multiple Server instances in the same process, matching
    lib/analytics.py's install_analytics() pattern.
    """
    if getattr(WhereClauseEvaluator, "_oftype_patched", False):
        return

    original_eval_el = WhereClauseEvaluator._eval_el

    def patched_eval_el(self: WhereClauseEvaluator, index: int, event: Any) -> Any:
        el = self.elements[index]
        if el.FilterOperator == ua.FilterOperator.OfType:
            target = self._eval_op(el.FilterOperands[0], event)
            return _is_type_or_subtype(self._aspace, event.EventType, target)
        return original_eval_el(self, index, event)

    patched_eval_el.__name__ = original_eval_el.__name__
    WhereClauseEvaluator._eval_el = patched_eval_el
    WhereClauseEvaluator._oftype_patched = True

# OPC UA Severity is a UInt16 in [1,1000]; Part 5 bands it Low(1-333)/
# Medium(334-666)/High(667-1000). Mirrors record_fault_transition()'s
# two-value severity scheme (lib/analytics.py) onto that scale.
_SEVERITY_MAP = {"critical": 900, "warning": 500}
_DEFAULT_SEVERITY = 500


async def fire_alarm_condition(
    server: Server,
    device_node: Any,  # ua.Node - the device's Object/FolderType node (event source)
    tag_node: Any,      # ua.Node - the tag's live Variable node (InputNode reference)
    tag_name: str,
    fault_type: str,
    is_active: bool,
) -> None:
    """Fire an AlarmConditionType event sourced from device_node.

    EventNotifier (and therefore event sourcing) is only a valid attribute on
    Object/View nodes per spec — not Variables — so the *device* is the
    event source, with the specific tag referenced via the condition's
    InputNode property, matching how real OPC UA servers typically model
    equipment-level alarms tied to a specific measured value.

    Best-effort: any failure is logged and swallowed, never allowed to break
    the caller's tick loop. Severity is derived from fault_type the same way
    record_fault_transition() derives it (lib/analytics.py) — kept as inline
    duplicated logic rather than a shared helper since the two are meant to
    tolerate independent failure/scope drift, not stay coupled.
    """
    try:
        message = f"{tag_name}: {fault_type} fault {'active' if is_active else 'cleared'}"
        severity_label = "critical" if fault_type == "offline" else "warning"
        severity = _SEVERITY_MAP.get(severity_label, _DEFAULT_SEVERITY)

        event = AlarmCondition(sourcenode=device_node.nodeid, message=message, severity=severity)
        event.ConditionName = tag_name
        event.Retain = is_active
        setattr(event, "InputNode", tag_node.nodeid)
        # EnabledState/ActiveState/AckedState/ConfirmedState are declared
        # LocalizedText (event_objects.py's add_variable calls) — a plain
        # Python str here breaks binary serialization (silently, in the
        # server's background publish loop, not at trigger() call time).
        setattr(event, "EnabledState/Id", True)
        event.EnabledState = ua.LocalizedText("Enabled")
        setattr(event, "ActiveState/Id", is_active)
        event.ActiveState = ua.LocalizedText("Active" if is_active else "Inactive")
        # No real ack workflow in v1 (see module docstring) — always reported
        # unacknowledged/unconfirmed rather than a stale or made-up state.
        setattr(event, "AckedState/Id", False)
        event.AckedState = ua.LocalizedText("Unacknowledged")
        setattr(event, "ConfirmedState/Id", False)
        event.ConfirmedState = ua.LocalizedText("Unconfirmed")

        # Fired at both the device node (direct per-device subscribers) and
        # the Server node (node-opcua's installAlarmMonitoring() default
        # pattern — see module docstring). get_event_generator()'s init()
        # adds (and resets to None) the NodeId property for any Condition
        # instance *after* construction — must be re-set on ev_gen.event
        # after each call, not on `event` beforehand, or it's silently
        # clobbered back to None. Re-registered via add_property (not plain
        # setattr) so it's present in data_types too — required by
        # to_event_fields() when a client's SelectClauses includes a bare
        # NodeId attribute selection (very common; most clients auto-include
        # it). Safe to reuse the same `event` object across both calls since
        # they're awaited sequentially and emitting_node is read at trigger
        # time, not capture time.
        for target_node in (device_node, ua.NodeId(ua.ObjectIds.Server)):
            ev_gen = await server.get_event_generator(event, emitting_node=target_node)
            ev_gen.event.add_property("NodeId", tag_node.nodeid, ua.VariantType.NodeId)
            await ev_gen.trigger(message=message)
    except Exception:
        log.exception("Failed to fire alarm condition for tag %r (non-fatal)", tag_name)

"""Simulation behavior strategies.

Physically extracted from src/legacy.py's `Behavior` hierarchy + `make_behavior()`
(continuing the GH #15 refactor, same "moved verbatim, no behavior changes"
standard as the Database and API-router extractions).
"""
from __future__ import annotations

import bisect
import json
import math
import random
from abc import ABC, abstractmethod
from typing import Any, Optional, Union

from .state import SimState

# How much EXTRA history (beyond the currently configured delay_seconds) FaultBehavior's rolling
# base-value history retains before pruning -- see FaultBehavior._prune_history(). Gives a
# reconfigured/increased Delay a small grace window rather than needing every increase to start
# from a completely fresh insufficient-history state; also what keeps a couple of ticks' worth
# of slack around delay_seconds=0.
_DELAY_HISTORY_SAFETY_MARGIN_SECONDS = 30.0
# Defensive cap on retained sample COUNT, independent of the time-based cutoff above -- guards
# against unbounded growth from a pathological config (an extremely large delay_seconds, or a
# tick rate far finer than this was ever sized for), never a limit that matters for realistic
# delay values.
_DELAY_HISTORY_MAX_SAMPLES = 100_000


def _dependencies():
    """
    Resolve src.dependencies lazily.

    IMPORTANT:
    Do not import src.dependencies at module import time.

    src.dependencies imports src.db.database, which itself reaches this
    module at import time via simulation.models.store -> simulation.models
    -> simulation.providers -> simulation.providers.builtin ->
    bacnet.app -> simulation.behaviors (this module). Importing
    src.dependencies eagerly here would re-enter that partially initialized
    chain and raise a circular-import ImportError -- caught by directly
    booting SimEngine, not by pyflakes (which checks each file in isolation
    and can't see multi-hop cycles like this one).

    FaultBehavior.compute() needs the live value of TICK_SECONDS, which is
    a mutable module global on src.dependencies (updated at runtime by
    _apply_settings_live()) -- resolving it lazily, and reading the
    attribute fresh on each call, is what keeps that live.
    """
    from .. import dependencies
    return dependencies


class Behavior(ABC):
    @abstractmethod
    def compute(self, state: SimState) -> Union[float, bool]:
        ...


class ConstantBehavior(Behavior):
    def __init__(self, params: dict):
        self.value = params.get("value", 0)

    def compute(self, state: SimState) -> Any:
        if isinstance(self.value, bool):
            return self.value
        return float(self.value)


class SineBehavior(Behavior):
    def __init__(self, params: dict):
        self.base = float(params.get("base", 20.0))
        self.amplitude = float(params.get("amplitude", 5.0))
        self.period_hours = float(params.get("period_hours", 24.0))
        self.phase_hours = float(params.get("phase_hours", 0.0))

    def compute(self, state: SimState) -> float:
        t = state.time_of_day + self.phase_hours
        return self.base + self.amplitude * math.sin(2 * math.pi * t / self.period_hours)


class NoiseBehavior(Behavior):
    def __init__(self, params: dict):
        self.base = float(params.get("base", 0.0))
        self.noise = float(params.get("noise", 1.0))

    def compute(self, state: SimState) -> float:
        return self.base + random.uniform(-self.noise, self.noise)


class RandomWalkBehavior(Behavior):
    def __init__(self, params: dict):
        self._value = float(params.get("value", 50.0))
        self.step = float(params.get("step", 1.0))
        self.min = float(params.get("min", 0.0))
        self.max = float(params.get("max", 100.0))

    def compute(self, state: SimState) -> float:
        self._value = max(self.min, min(self.max, self._value + random.uniform(-self.step, self.step)))
        return self._value


class ManualBehavior(Behavior):
    # Recognized boolean-ish string forms -- covers not just literal JSON
    # true/false but also the ON/OFF vocabulary this app itself displays
    # binary points as (see admin/src/format.ts's formatPresentValue) and
    # BACnet's own active/inactive enumeration names. Anything NOT in this
    # set falls through to float() -- correct for analog manual overrides
    # (e.g. "72.5"), where treating "false"-shaped text as special would be
    # wrong. Case/whitespace-insensitive since this can come from a free-
    # text UI field, not just a typed API caller.
    _TRUE_WORDS = ("true", "on", "active", "yes")
    _FALSE_WORDS = ("false", "off", "inactive", "no")

    @classmethod
    def _coerce(cls, raw: Any) -> Any:
        if raw is None:
            return 0
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            normalized = raw.strip().lower()
            if normalized in cls._TRUE_WORDS:
                return True
            if normalized in cls._FALSE_WORDS:
                return False
        return float(raw)

    def __init__(self, params: dict, stored_value: Any = None):
        self._value = self._coerce(params.get("value", stored_value))

    def set(self, v: Any) -> None:
        self._value = self._coerce(v)

    def compute(self, state: SimState) -> Any:
        return self._value


class DailyPatternBehavior(Behavior):
    """Returns different values based on time-of-day blocks (occupied/unoccupied
    scheduling). Not to be confused with a real BACnet Schedule object (see
    bacnet_schedule.py) — this is purely a value-simulation behavior, stored
    under the historical behavior name "schedule" for backward compatibility
    with existing projects/seed data, but renamed at the Python-class level
    now that real BACnet Schedule objects exist too."""

    @staticmethod
    def _parse_time(t: str) -> float:
        try:
            h, m = t.split(":")
            return int(h) + int(m) / 60.0
        except Exception:
            return 0.0

    def __init__(self, params: dict):
        self.default = float(params.get("default", 0))
        raw_blocks = params.get("blocks", [])
        self.blocks = sorted(
            [{"start": self._parse_time(b.get("start", "00:00")), "value": float(b.get("value", 0))}
             for b in raw_blocks if isinstance(b, dict)],
            key=lambda b: b["start"],
        )

    def compute(self, state: SimState) -> float:
        current = state.time_of_day % 24
        value = self.default
        for block in self.blocks:
            if current >= block["start"]:
                value = block["value"]
            else:
                break
        return value


class RampBehavior(Behavior):
    """Linearly ramps from one value to another over a fixed duration, optionally repeating."""

    def __init__(self, params: dict):
        self.from_val = float(params.get("from", 0))
        self.to_val = float(params.get("to", 100))
        self.duration_seconds = float(params.get("duration_minutes", 60)) * 60
        self.repeat = bool(params.get("repeat", True))

    def compute(self, state: SimState) -> float:
        if self.duration_seconds <= 0:
            return self.to_val
        if self.repeat:
            t = state.elapsed_seconds % self.duration_seconds
        else:
            t = min(state.elapsed_seconds, self.duration_seconds)
        frac = t / self.duration_seconds
        return self.from_val + (self.to_val - self.from_val) * frac


class FaultBehavior(Behavior):
    """Wraps a base behavior and randomly injects fault conditions (spike, stuck, offline, bias,
    drift, delay).

    spike/stuck freeze the reported value at a fixed fault_value for the duration of the fault
    -- the base behavior's own compute() is simply not called while active. `bias`/`drift` are
    different on purpose: both are additive on top of whatever the base behavior would have
    produced THIS tick, so the underlying signal (noise jitter, a random walk, a sine curve,
    ...) keeps evolving normally while the fault is active, never frozen. `bias` is a fixed
    offset for the whole fault duration; `drift` is the same idea
    but the offset itself grows/shrinks linearly with elapsed time since this fault event
    started (see `_fault_start_elapsed` below), clamped to +/-`maximum_drift`. See
    `fault_value`'s reuse below: for every fault_type it's the same generic stored number, just
    interpreted differently (an absolute override for spike/stuck, an additive offset for bias)
    -- no separate field. `drift` needs two independent numbers (a rate AND a clamp magnitude)
    that don't fit that single-field reuse without ambiguity, so it gets its own two fields
    instead (`drift_rate_per_minute`, `maximum_drift`) -- both optional/defaulted, so existing
    spike/stuck/offline/bias configs (which never had these keys) are unaffected.

    `delay` is a pure TIME shift, not a magnitude change: output(t) = base_value(t -
    delay_seconds) -- see `_record_base`/`_history_value_at_or_before` below. Deliberately NOT
    a frozen/held value (that's `stuck`) and NOT first-order-lag/smoothing dynamics (a distinct,
    unimplemented future fault type) -- delay looks up a genuine PAST sample of the base signal,
    it never blends toward one. `delay_seconds` is its own field for the same reason
    drift needed its own fields: not a sensible reuse of the single generic `fault_value`
    (a magnitude), which stays meaningless for delay.

    Rolling history tradeoff (see `_record_base`): rather than inventing a second, delay-only
    state-tracking mechanism, EVERY existing call site that already invokes
    `self._inner.compute(state)` (the plain-inactive passthrough, and bias/drift's own active
    branches) now routes through `_record_base`, which also appends the sample to
    `self._history` and prunes it. This means a small bounded history (bounded by
    delay_seconds + a fixed safety margin, see `_prune_history`) is maintained for EVERY
    fault_type whenever the base behavior is actually evaluated, not only when fault_type ==
    "delay" -- cheap (a short Python list, appended/pruned once per tick) and exactly what makes
    history already available the moment a user switches an existing fault to Delay, or
    switches Delay on via activation, without a separate pre-warming mechanism. spike/stuck
    still never call `self._inner.compute()` while active (unchanged, see above), so history has
    correspondingly sparser coverage during another fault type's own active window -- the
    lookup's own oldest-available-sample fallback (never 0/None/NaN/an exception) already
    absorbs that gracefully.

    `offline` is a BACnet-PROTOCOL-STATUS fault, not a value fault -- it does NOT force
    Present_Value to 0 (0 is a legitimate reading for plenty of real points -- 0 CFM airflow, 0%
    damper position -- and conflating "unavailable" with "reads as zero" is exactly the wrong
    signal to teach an FDD model). Present_Value keeps reflecting the live base signal (same
    "never freeze" treatment as bias/drift/delay, via `_record_base`) for as long as offline is
    active; the point's actual unavailability is signaled entirely through BACnet Reliability +
    Status_Flags.fault, applied every tick by SimEngine's own tick loop (engine.py), which
    checks THIS instance's `fault_type`/`_fault_active` to decide -- see that method's own
    comment for why that one additional per-tick side effect self-corrects on recovery, on
    switching to a different fault_type, and when Behavior != "fault", all for free, with no new
    runtime state needed here. (Before this, `offline` returned a hardcoded 0.0 -- a pre-existing
    simulator-specific approximation with no BACnet-protocol meaning at all; nothing in this
    repository -- no seed data, no persisted project, no test predating this change -- was found
    depending on that exact value, so this replaces it outright rather than adding a legacy/
    compatibility mode for it.)

    `activation_mode` ("intermittent" | "always") is a SEPARATE, orthogonal concern from
    fault_type -- deliberately kept that way (see compute() below): it only ever decides WHETHER
    the fault is active this tick, never WHAT value gets reported once it is. "intermittent" is
    the existing MTBF/fault_duration_seconds scheduler, completely unchanged. "always" skips
    that scheduler entirely -- no MTBF wait, no automatic expiry -- and stays continuously
    active for as long as this object's Behavior stays "fault". Missing activation_mode (every
    config saved before this feature existed) defaults to "intermittent", so no saved
    spike/stuck/offline/bias configuration changes behavior. "always" + spike is a UI-level
    restriction, not a backend one (see ObjectDrawer.vue) -- spike's own defining
    characteristic is a single momentary bad reading; continuously active would just degenerate
    into indistinguishable-from-stuck (spike's own active-branch return value, `fault_value`, is
    already identical to stuck's), so it's disabled where a user would pick it rather than
    inventing new "continuous spike" semantics that don't exist in the current implementation."""

    def __init__(self, params: dict):
        self._base_behavior_name = params.get("base_behavior", "constant")
        self._base_params = params.get("base_params", {"value": 0})
        self._inner: Optional[Behavior] = None
        self.fault_type = params.get("fault_type", "spike")
        self.fault_value = float(params.get("fault_value", 999))
        self.mtbf_minutes = float(params.get("mtbf_minutes", 60))
        self.fault_duration_seconds = float(params.get("fault_duration_seconds", 30))
        # drift-only config -- see class docstring for why these are separate fields rather
        # than overloading fault_value. Defaults describe a modest, bounded drift so a freshly
        # created drift fault (before a user tunes it) never produces an unrealistic unbounded
        # value.
        self.drift_rate_per_minute = float(params.get("drift_rate_per_minute", 0.1))
        self.maximum_drift = float(params.get("maximum_drift", 2.0))
        # delay-only config -- see class docstring. Clamped to >= 0 (never rejected/raised --
        # matches how every other fault sub-field in this class is handled: no field here has
        # ever had API/schema-level range validation, see schemas.py's ObjectCreate, which only
        # validates behavior_params as JSON). A negative value would otherwise mean "look into
        # the future," which has no defined meaning.
        self.delay_seconds = max(0.0, float(params.get("delay_seconds", 30.0)))
        # Rolling (elapsed_seconds, value) history of the base behavior's own output -- see
        # _record_base/_history_value_at_or_before/_prune_history and the class docstring's own
        # "rolling history tradeoff" note. Never persisted (runtime-only, exactly like
        # _fault_active/_fault_end_elapsed) and never database-backed.
        self._history: list[tuple[float, Any]] = []
        # Backward-compat default: any config saved before this feature existed has no
        # activation_mode key at all and must resolve to "intermittent" -- see class docstring.
        self.activation_mode = params.get("activation_mode") or "intermittent"
        self._fault_active = False
        self._fault_end_elapsed: float = -1.0
        # When THIS fault event became active (state.elapsed_seconds at that moment) -- drift's
        # own clock. Deliberately the same activation site that already sets
        # _fault_end_elapsed below, not a second/independent timer: every new fault event
        # (including a later, unrelated one after this one ends) resets it, which is exactly
        # what makes a fresh drift event start back at offset 0 instead of continuing a
        # previous event's accumulated drift.
        self._fault_start_elapsed: float = -1.0
        # Detects a LIVE activation_mode change on an already-running instance (a user edits
        # this object's fault config mid-simulation and flips Always Active <-> Intermittent) --
        # see compute()'s own transition check. None on construction so the very first
        # compute() call is never mistaken for a transition.
        self._last_activation_mode: Optional[str] = None

    def compute(self, state: SimState) -> float:
        if self._inner is None:
            self._inner = make_behavior(self._base_behavior_name, json.dumps(self._base_params))

        if self._last_activation_mode is not None and self._last_activation_mode != self.activation_mode:
            # The mode itself just changed (either direction) -- never carry the other mode's
            # timing state across the transition. Reinitializing to "inactive, no end/start"
            # here and letting the activation logic below run immediately afterward gives both
            # directions exactly the behavior the spec calls for: Intermittent -> Always Active
            # activates on THIS evaluation (no leftover MTBF wait, handled by the "always"
            # branch below unconditionally activating when inactive); Always Active ->
            # Intermittent starts a completely fresh MTBF wait (handled by the "intermittent"
            # branch below, which only reactivates via its own random roll) instead of being
            # stuck forever at the old _fault_end_elapsed = +inf.
            self._fault_active = False
            self._fault_end_elapsed = -1.0
            self._fault_start_elapsed = -1.0
        self._last_activation_mode = self.activation_mode

        # ── Activation: decide whether the fault is active THIS tick. Deliberately the ONLY
        # place fault_type is inspected is the one exception already documented on the class
        # (spike's zero-duration one-tick special case, unchanged) -- everything else here is
        # generic across every fault_type, current or future. ────────────────────────────────
        if self.activation_mode == "always":
            if not self._fault_active:
                # First tick of this event (fresh instance, or just transitioned in above) --
                # activate unconditionally, no MTBF roll, no expiry. Only set once per event
                # (guarded by `not self._fault_active`): drift's own clock must NOT reset on
                # every tick just because "always" re-evaluates as active every time.
                self._fault_active = True
                self._fault_start_elapsed = state.elapsed_seconds
                self._fault_end_elapsed = math.inf
        else:
            # intermittent -- existing MTBF/fault_duration_seconds scheduler, unchanged.
            if self._fault_active and state.elapsed_seconds > self._fault_end_elapsed:
                self._fault_active = False

            if not self._fault_active:
                # Ticks occur every TICK_SECONDS, not every second, so scale the
                # per-tick probability accordingly to make mtbf_minutes accurate.
                prob_per_tick = _dependencies().TICK_SECONDS / max(1.0, self.mtbf_minutes * 60.0)
                if random.random() < prob_per_tick:
                    self._fault_active = True
                    self._fault_start_elapsed = state.elapsed_seconds
                    if self.fault_type == "spike":
                        self._fault_end_elapsed = state.elapsed_seconds
                    else:
                        self._fault_end_elapsed = state.elapsed_seconds + self.fault_duration_seconds

        # ── Transformation: apply the selected fault_type using the activation decided above.
        # Unchanged from before activation_mode existed -- this block has no idea whether it
        # got here via "always" or "intermittent". ─────────────────────────────────────────
        if self._fault_active:
            if self.fault_type == "offline":
                # Present_Value is a pure passthrough of the live base signal -- see class
                # docstring's "offline is a BACnet-protocol-status fault, not a value fault"
                # note. The actual unavailability signal (Reliability=communication-failure,
                # Status_Flags.fault=1) is applied at the BACnet layer, not here -- this class
                # has no BACnet object reference at all, by design (see engine.py's tick()).
                return self._record_base(state)
            if self.fault_type == "bias":
                # Additive offset on top of the base behavior's OWN value for this tick --
                # deliberately still evaluating/recording the base behavior (not skipped, unlike
                # spike/stuck/offline above) so a dynamic base (noise/random_walk/sine/...)
                # keeps changing normally while the fault is active, instead of freezing.
                return float(self._record_base(state)) + self.fault_value
            if self.fault_type == "drift":
                # Same "add to the base's own live value" idea as bias, except the offset
                # itself is a function of elapsed time since THIS fault event started
                # (_fault_start_elapsed, set once per activation above) -- never simulation
                # startup, object creation, or a previous fault event. Linear only (no
                # exponential/random-walk growth), and clamped to +/-maximum_drift so a
                # long-running fault can't drift unboundedly.
                elapsed_fault_minutes = (state.elapsed_seconds - self._fault_start_elapsed) / 60.0
                raw_offset = self.drift_rate_per_minute * elapsed_fault_minutes
                max_drift = abs(self.maximum_drift)
                offset = max(-max_drift, min(max_drift, raw_offset))
                return float(self._record_base(state)) + offset
            if self.fault_type == "delay":
                # Pure time shift, not a magnitude change -- output(t) = base_value(t -
                # delay_seconds). Still records THIS tick's own base value first (same as bias/
                # drift above) so the signal keeps advancing/contributing fresh history while
                # the fault is active -- both to honor "the Base Behavior must continue running
                # normally" and so a delay_seconds increase mid-fault has a chance to accumulate
                # the extra history it will need going forward. Deliberately NOT wrapped in
                # float(): delay is a pure passthrough of whatever the base behavior actually
                # produced at that past instant (float OR bool for a binary/manual base), never
                # an arithmetic result -- unlike bias/drift, which must add a numeric offset.
                self._record_base(state)
                target_time = state.elapsed_seconds - self.delay_seconds
                return self._history_value_at_or_before(target_time)
            return self.fault_value

        return self._record_base(state)

    def _record_base(self, state: SimState) -> Any:
        """Evaluates the base behavior for THIS tick and appends it to the rolling
        (timestamp, value) history delay's own lookup reads from. The ONE place
        self._inner.compute() is invoked from now on (previously called directly at each of
        this method's three call sites) -- see class docstring's "rolling history tradeoff."
        Never called while spike/stuck/offline are active (those branches return before
        reaching here, unchanged) -- delay's history is simply sparser during another fault
        type's own active window, which _history_value_at_or_before's oldest-sample fallback
        already handles."""
        value = self._inner.compute(state)
        self._history.append((state.elapsed_seconds, value))
        self._prune_history(state.elapsed_seconds)
        return value

    def _prune_history(self, now: float) -> None:
        """Keeps history newer than `now - delay_seconds - safety_margin` (see module-level
        _DELAY_HISTORY_SAFETY_MARGIN_SECONDS), recomputed from the CURRENT delay_seconds every
        call -- not fixed at construction -- so increasing Delay going forward automatically
        retains a longer window from then on. History already discarded under a previously
        smaller window is simply gone; that's the documented, expected insufficient-history
        case (see _history_value_at_or_before), not a bug. A count-based cap is enforced
        afterward as a defensive backstop against a pathological config, independent of the
        time-based cutoff."""
        cutoff = now - self.delay_seconds - _DELAY_HISTORY_SAFETY_MARGIN_SECONDS
        idx = bisect.bisect_right(self._history, cutoff, key=lambda entry: entry[0])
        if idx > 0:
            del self._history[:idx]
        if len(self._history) > _DELAY_HISTORY_MAX_SAMPLES:
            del self._history[: len(self._history) - _DELAY_HISTORY_MAX_SAMPLES]

    def _history_value_at_or_before(self, target_time: float) -> Any:
        """Phase-one lookup, deliberately simple and deterministic: the newest retained sample
        with timestamp <= target_time, no interpolation. `_record_base` always appends the
        current tick's own sample before this is ever called from the delay branch, so
        self._history is never actually empty here in practice -- the `not self._history` guard
        is a defensive fallback only. When target_time predates every retained sample (delay
        just activated before enough history existed, or delay_seconds was just increased past
        the retention window), returns the OLDEST available sample rather than 0/None/NaN or
        raising -- the documented insufficient-history policy."""
        if not self._history:
            return 0.0
        idx = bisect.bisect_right(self._history, target_time, key=lambda entry: entry[0])
        if idx == 0:
            return self._history[0][1]
        return self._history[idx - 1][1]


def make_behavior(behavior: str, params_json: str, manual_value: Any = None) -> Behavior:
    try:
        params = json.loads(params_json) if params_json else {}
    except Exception:
        params = {}
    if behavior == "constant":
        return ConstantBehavior(params)
    if behavior == "sine":
        return SineBehavior(params)
    if behavior == "noise":
        return NoiseBehavior(params)
    if behavior == "random_walk":
        return RandomWalkBehavior(params)
    if behavior == "manual":
        return ManualBehavior(params, manual_value)
    if behavior == "schedule":
        return DailyPatternBehavior(params)
    if behavior == "ramp":
        return RampBehavior(params)
    if behavior == "fault":
        return FaultBehavior(params)
    if behavior == "raw":
        # "raw" only has meaning for a provider-owned point once its FMU
        # has actually produced a value (see SimEngine._apply_fmu_behavior,
        # which substitutes the live raw value directly and never calls
        # this instance's compute() at all for "raw"). Before that first
        # tick -- e.g. the one-time initial-value seed in
        # SimEngine._create_object() -- there is no raw value yet, so this
        # instance's own compute() falls back to whatever's stored in
        # behavior_params, exactly like "constant" does. Critically, this
        # must NOT hit the generic "unrecognized -> ConstantBehavior({
        # "value": 0})" fallback below, which discards params entirely --
        # that silently zeroed every "raw" point's seed value (and any
        # weighted-average aggregate depending on it) when this branch was
        # missing.
        return ConstantBehavior(params)
    return ConstantBehavior({"value": 0})

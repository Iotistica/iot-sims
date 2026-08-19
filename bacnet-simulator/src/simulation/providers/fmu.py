from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .base import ProviderStatus, SimulationContext, SimulationProvider, ValidationResult


log = logging.getLogger("bacnet-sim")


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt_resolve_value(value: Any) -> str:
    numeric = _as_float(value)
    return f"{numeric:.2f}" if numeric is not None else str(value)


class FMUInputResolutionError(RuntimeError):
    """Raised when a configured Point-mapped input has no live value to
    initialize with. Never caught to substitute a metadata/catalog default —
    initialize() must abort so the caller (registration/recovery) retries
    later instead of seeding the FMU with a value the operator didn't
    configure.
    """


class FMUAggregateStepError(RuntimeError):
    """Raised by step-time aggregate resolution when one or more configured
    members are missing or non-numeric. Unlike an ordinary Point mapping —
    which tolerates a missing value at step time by falling back to the FMU
    runtime's own metadata default — an aggregate must never compute its
    operation (e.g. MAX) over a partial member set: that can silently
    produce a valid-looking but semantically wrong control signal (e.g. the
    truly-most-open VAV happening to be the member that went stale would
    make MAX(remaining) quietly under-report demand). So the whole step
    fails instead, with no HTTP call to the FMU runtime and no other input
    silently applied for that step.
    """


@dataclass(frozen=True)
class FMUPointBinding:
    point_id: int
    variable: str
    direction: str


# "max" and "weighted_average" are implemented; the tuple/dataclass shape
# (an operation string plus a list of source points) is deliberately generic
# so min/avg/sum can be added later without changing the aggregate's
# representation.
_SUPPORTED_AGGREGATE_OPERATIONS = {"max", "weighted_average"}


@dataclass(frozen=True)
class FMUAggregateInput:
    """An FMU input variable driven by an operation ("max" or
    "weighted_average") over several BACnet points' live values, rather than
    exactly one point or a constant. See FMUAggregateStepError for why "max"
    never tolerates partial resolution.

    weight_point_ids is only meaningful for operation="weighted_average": it
    is a second point-id list, positionally paired with point_ids (index i's
    weight is weight_point_ids[i]), one weight point per value point. It is
    empty for "max" (or any future non-weighted operation). Unlike "max",
    weighted_average tolerates a partial member set at resolution time --
    see _resolve_weighted_average's docstring for why a per-pair fault
    doesn't have the same "silently wrong" risk max's docstring warns about.
    """
    variable: str
    operation: str
    point_ids: tuple[int, ...]
    weight_point_ids: tuple[int | None, ...] = ()


@dataclass(frozen=True)
class FMURuntimeResponse:
    status_code: int
    raw_body: str
    body: dict[str, Any]


class FMURuntimeClient:
    def __init__(self, base_url: str, timeout_s: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        query: Mapping[str, Any] | None = None,
    ) -> FMURuntimeResponse:
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{urlencode(query)}"

        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(dict(body)).encode("utf-8")
            headers["Content-Type"] = "application/json"

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(request, timeout=self.timeout_s) as response:
                status_code = int(response.status)
                raw = response.read().decode("utf-8")
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            body: dict[str, Any] = {}
            try:
                decoded = json.loads(raw) if raw else {}
                if isinstance(decoded, dict):
                    body = decoded
            except json.JSONDecodeError:
                pass
            return FMURuntimeResponse(
                status_code=int(exc.code),
                raw_body=raw,
                body=body,
            )
        except URLError as exc:
            raise RuntimeError(f"FMU runtime is unreachable at {self.base_url}: {exc.reason}") from exc

        if not raw:
            return FMURuntimeResponse(
                status_code=status_code,
                raw_body="",
                body={},
            )
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"FMU runtime returned non-JSON response: {raw[:200]}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(f"FMU runtime returned JSON {type(decoded).__name__}, expected object")
        return FMURuntimeResponse(
            status_code=status_code,
            raw_body=raw,
            body=decoded,
        )

    def health(self) -> dict[str, Any]:
        response = self._request("GET", "/health")
        if response.status_code >= 400:
            raise RuntimeError(
                f"FMU runtime HTTP {response.status_code}: {response.raw_body}"
            )
        return response.body

    def initialize(self, model_id: str, inputs: Mapping[str, Any] | None = None) -> dict[str, Any]:
        response = self._request(
            "POST",
            f"/models/{model_id}/initialize",
            body={"inputs": dict(inputs or {})},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"FMU runtime HTTP {response.status_code}: {response.raw_body}"
            )
        result = response.body
        session_id = result.get("session_id")
        if not session_id:
            raise RuntimeError("FMU runtime initialize response did not include session_id")
        return result

    def step(self, model_id: str, payload: Mapping[str, Any]) -> FMURuntimeResponse:
        return self._request("POST", f"/models/{model_id}/step", body=payload)

    def terminate(self, model_id: str, session_id: str) -> None:
        response = self._request(
            "POST",
            f"/models/{model_id}/terminate",
            query={"session_id": session_id},
        )
        if response.status_code >= 400:
            raise RuntimeError(
                f"FMU runtime HTTP {response.status_code}: {response.raw_body}"
            )


class FMUSimulationProvider(SimulationProvider):
    """Drives a remote FMU runtime API and maps BACnet points to API fields."""

    def __init__(
        self,
        *,
        runtime_url: str,
        model: str,
        bindings: list[FMUPointBinding],
        aggregate_inputs: list[FMUAggregateInput] | None = None,
        input_defaults: Mapping[str, Any] | None = None,
        timeout_s: float = 20.0,
        input_variables: set[str] | None = None,
        output_variables: set[str] | None = None,
    ) -> None:
        self._runtime_url = runtime_url.rstrip("/")
        self._model = model
        self._bindings = bindings
        self._aggregate_inputs = list(aggregate_inputs or [])
        self._input_defaults = dict(input_defaults or {})
        self._timeout_s = float(timeout_s)
        self._input_variables = set(input_variables or ())
        self._output_variables = set(output_variables or ())
        self._context: SimulationContext | None = None
        self._client = FMURuntimeClient(self._runtime_url, self._timeout_s)
        self._session_id: str | None = None
        self._inputs: dict[int, Any] = {}
        self._outputs: dict[int, Any] = {}
        self._status = ProviderStatus.NOT_CONFIGURED
        self._error: str | None = None
        self._first_step_logged = False
        self._last_runtime_time: float | None = None
        self._last_step_inputs: dict[str, dict[str, Any]] = {}
        self._last_step_outputs: dict[str, dict[str, Any]] = {}
        self._step_sequence = 0
        self._step_in_progress = False
        self._last_step_id: str | None = None
        self._last_http_status: int | None = None
        self._last_raw_response = ""
        self._runtime_state: str | None = None
        self._warmup_progress: float | None = None
        self._warmup_seconds: float | None = None

    def initialize(self, context: SimulationContext) -> None:
        self._context = context
        self._inputs = dict(context.metadata.get("initial_point_inputs") or {})
        self._outputs = {}
        self._error = None
        self._first_step_logged = False
        self._last_runtime_time = None
        self._last_step_inputs = {}
        self._last_step_outputs = {}
        self._step_sequence = 0
        self._step_in_progress = False
        self._last_step_id = None
        self._last_http_status = None
        self._last_raw_response = ""
        self._runtime_state = None
        self._warmup_progress = None
        self._warmup_seconds = None
        try:
            init_inputs, unresolved = self._resolve_init_inputs()
            if unresolved:
                raise FMUInputResolutionError(
                    "Cannot initialize: Point-mapped input(s) have no live "
                    "value yet: " + "; ".join(unresolved)
                )
            health = self._client.health()
            if health.get("status") not in (None, "ok"):
                raise RuntimeError(f"FMU runtime health is {health.get('status')!r}")
            self._session_id = self._initialize_runtime_session(
                init_inputs,
                reason="initialize",
            )
            self._status = ProviderStatus.READY
            log.info(
                "FMU session initialized: session=%s model=%s runtime=%s config=%s",
                self._session_id,
                self._model,
                self._runtime_url,
                self._metadata_summary(),
            )
        except Exception as exc:
            self._session_id = None
            self._error = str(exc)
            self._status = ProviderStatus.ERROR
            raise

    def _resolve_init_inputs(self) -> tuple[dict[str, Any], list[str]]:
        """Build the initialize()/warmup input payload: Constant-mode inputs
        pass through unchanged; Point-mode inputs must already have a live
        value in self._inputs (seeded by the caller from
        context.metadata["initial_point_inputs"]) or they're reported as
        unresolved instead of silently falling back to a metadata default.
        """
        inputs: dict[str, Any] = dict(self._input_defaults)
        unresolved: list[str] = []
        simulation_model_id = (
            self._context.metadata.get("simulation_model_id") if self._context else None
        )
        for binding in self._bindings:
            if binding.direction != "input":
                continue
            metadata = self._binding_metadata(binding)
            source_label = f"{metadata.get('device_name')}/{metadata.get('point_name')}"
            value = self._inputs.get(binding.point_id)
            if value is None:
                log.warning(
                    "FMU INPUT RESOLVE input=%s mode=point source=%s value=MISSING "
                    "device_id=%s object=%s:%s point_id=%s simulation_model_id=%s "
                    "model=%s -- cannot initialize without a resolved value for "
                    "this Point mapping",
                    binding.variable,
                    source_label,
                    metadata.get("device_id"),
                    metadata.get("object_type"),
                    metadata.get("object_instance"),
                    binding.point_id,
                    simulation_model_id,
                    self._model,
                )
                unresolved.append(
                    f"{binding.variable} <- {source_label} (point {binding.point_id})"
                )
                continue
            inputs[binding.variable] = value
            log.info(
                "FMU INPUT RESOLVE input=%s mode=point source=%s value=%s",
                binding.variable,
                source_label,
                _fmt_resolve_value(value),
            )

        for agg in self._aggregate_inputs:
            aggregate_value, detail, _diagnostics = self._resolve_one_aggregate(agg)
            if detail is not None:
                log.warning(
                    "FMU AGGREGATE variable=%s operation=%s point_ids=%s -- "
                    "cannot initialize, %s",
                    agg.variable,
                    agg.operation,
                    list(agg.point_ids),
                    detail,
                )
                unresolved.append(detail)
                continue
            inputs[agg.variable] = aggregate_value
            log.info(
                "FMU AGGREGATE variable=%s operation=%s aggregate=%s",
                agg.variable,
                agg.operation,
                aggregate_value,
            )

        return inputs, unresolved

    def _resolve_one_aggregate(
        self, agg: FMUAggregateInput,
    ) -> tuple[float | None, str | None, dict[str, Any]]:
        """Resolves one aggregate to (value, None, diagnostics) on success or
        (None, failure_detail, diagnostics) on failure, dispatching on
        agg.operation. Shared by _resolve_init_inputs and
        _build_step_payload so both call sites stay in lockstep as
        operations are added -- only their own differing log level /
        unresolved-vs-raise handling lives at the call site. diagnostics is
        operation-shaped (raw_values for max, pairs for weighted_average)
        so callers can build a full input_report entry without needing
        their own per-operation branch."""
        if agg.operation == "weighted_average":
            value, pair_diagnostics = self._resolve_weighted_average(agg)
            diagnostics: dict[str, Any] = {"pairs": pair_diagnostics}
            if value is None:
                return None, self._weighted_average_failure_detail(agg, pair_diagnostics), diagnostics
            return value, None, diagnostics

        raw_values, missing, non_numeric = self._resolve_aggregate_source_values(agg)
        if missing or non_numeric:
            return None, self._aggregate_failure_detail(agg, missing, non_numeric), {}
        return self._compute_aggregate(agg, raw_values), None, {"raw_values": raw_values}

    def _initialize_runtime_session(
        self,
        inputs: Mapping[str, Any] | None,
        *,
        reason: str,
    ) -> str:
        result = self._client.initialize(self._model, inputs or {})
        session_id = str(result["session_id"])
        self._runtime_state = str(result.get("state") or "")
        self._warmup_progress = _as_float(result.get("warmup_progress"))
        self._warmup_seconds = _as_float(result.get("warmup_seconds"))
        log.info(
            "FMU runtime session created: session=%s model=%s runtime=%s "
            "reason=%s state=%s warmup_progress=%s warmup_seconds=%s inputs=%s",
            session_id,
            self._model,
            self._runtime_url,
            reason,
            self._runtime_state,
            self._warmup_progress,
            self._warmup_seconds,
            dict(inputs or {}),
        )
        return session_id

    def start(self) -> None:
        if self._status in (ProviderStatus.READY, ProviderStatus.PAUSED):
            self._status = ProviderStatus.RUNNING

    def pause(self) -> None:
        if self._status == ProviderStatus.RUNNING:
            self._status = ProviderStatus.PAUSED

    def stop(self) -> None:
        if self._session_id:
            try:
                self._client.terminate(self._model, self._session_id)
            except Exception as exc:
                self._error = str(exc)
            finally:
                self._session_id = None
        self._status = ProviderStatus.STOPPED if self._context is not None else ProviderStatus.NOT_CONFIGURED

    def reset(self) -> None:
        self.stop()
        self._inputs = {}
        self._outputs = {}
        if self._context is not None:
            self.initialize(self._context)

    def _metadata_summary(self) -> dict[str, Any]:
        metadata = self._context.metadata if self._context else {}
        return {
            "provider_id": metadata.get("provider_id"),
            "simulation_model_id": metadata.get("simulation_model_id"),
            "name": metadata.get("name"),
            "model_type": metadata.get("model_type"),
            "participant_device_ids": metadata.get("participant_device_ids"),
            "input_device_ids": metadata.get("input_device_ids"),
            "output_device_ids": metadata.get("output_device_ids"),
        }

    def _provider_id(self) -> str | None:
        metadata = self._context.metadata if self._context else {}
        value = metadata.get("provider_id")
        return str(value) if value is not None else None

    def _device_ids(self) -> list[int]:
        metadata = self._context.metadata if self._context else {}
        values = metadata.get("output_device_ids") or metadata.get("participant_device_ids") or []
        return [int(value) for value in values]

    def _output_bindings_by_variable(self) -> dict[str, FMUPointBinding]:
        return {
            binding.variable: binding
            for binding in self._bindings
            if binding.direction == "output"
        }

    def _binding_metadata(self, binding: FMUPointBinding) -> dict[str, Any]:
        if not self._context:
            return {}
        for item in self._context.metadata.get("bindings", []):
            if (
                int(item.get("point_id", -1)) == binding.point_id
                and str(item.get("variable")) == binding.variable
                and str(item.get("direction")) == binding.direction
            ):
                return {
                    key: item.get(key)
                    for key in (
                        "point_name",
                        "device_name",
                        "device_id",
                        "object_type",
                        "object_instance",
                        "units",
                    )
                    if item.get(key) is not None
                }
        return {}

    def _aggregate_member_metadata(self, agg: FMUAggregateInput, point_id: int) -> dict[str, Any]:
        """Mirrors _binding_metadata() but keyed off point_id + the
        aggregate's own variable, since aggregate members don't have their
        own FMUPointBinding -- model_runtime.py adds one context.metadata
        "bindings" entry per member (variable=agg.variable,
        direction="input", point_id=member) precisely so this lookup works
        the same way for both ordinary and aggregate-member points."""
        if not self._context:
            return {}
        for item in self._context.metadata.get("bindings", []):
            if (
                int(item.get("point_id", -1)) == point_id
                and str(item.get("variable")) == agg.variable
                and str(item.get("direction")) == "input"
            ):
                return {
                    key: item.get(key)
                    for key in (
                        "point_name",
                        "device_name",
                        "device_id",
                        "object_type",
                        "object_instance",
                        "units",
                    )
                    if item.get(key) is not None
                }
        return {}

    def _resolve_aggregate_source_values(
        self, agg: FMUAggregateInput,
    ) -> tuple[list[float], list[int], list[int]]:
        """Reads every configured member's live value from self._inputs (the
        same dict ordinary Point bindings read from). Returns
        (raw_values, missing_point_ids, non_numeric_point_ids) -- missing
        and non-numeric are tracked separately so their error messages stay
        distinct, and are always checked against every configured member
        (never short-circuiting), since a caller needs the complete failure
        picture to build one clear diagnostic."""
        raw_values: list[float] = []
        missing: list[int] = []
        non_numeric: list[int] = []
        for point_id in agg.point_ids:
            if point_id not in self._inputs:
                missing.append(point_id)
                continue
            numeric = _as_float(self._inputs[point_id])
            if numeric is None:
                non_numeric.append(point_id)
                continue
            raw_values.append(numeric)
        return raw_values, missing, non_numeric

    @staticmethod
    def _compute_aggregate(agg: FMUAggregateInput, raw_values: list[float]) -> float:
        if agg.operation == "max":
            return max(raw_values)
        raise ValueError(f"Unsupported aggregate operation: {agg.operation!r}")

    def _aggregate_failure_detail(
        self, agg: FMUAggregateInput, missing: list[int], non_numeric: list[int],
    ) -> str:
        parts = []
        if missing:
            labels = ", ".join(
                f"point {pid} ({self._aggregate_member_metadata(agg, pid).get('device_name')}/"
                f"{self._aggregate_member_metadata(agg, pid).get('point_name')})"
                for pid in missing
            )
            parts.append(f"missing value for {labels}")
        if non_numeric:
            labels = ", ".join(
                f"point {pid} (value={self._inputs.get(pid)!r})" for pid in non_numeric
            )
            parts.append(f"non-numeric value for {labels}")
        return (
            f"{agg.variable} <- aggregate({agg.operation}) of {list(agg.point_ids)}: "
            + "; ".join(parts)
        )

    def _resolve_weighted_average(
        self, agg: FMUAggregateInput,
    ) -> tuple[float | None, list[dict[str, Any]]]:
        """Computes sum(value[i]*weight[i]) / sum(weight[i]) over agg's
        value/weight point pairs. Returns (result, pair_diagnostics);
        result is None when there is no valid pair to divide by (every pair
        was invalid, or every valid weight was exactly 0) -- the caller must
        treat None as an aggregate failure, the weighted_average counterpart
        to "max" raising on a missing/non-numeric member.

        Unlike "max", an individual pair with a missing/non-numeric value,
        missing/non-numeric weight, or a negative weight is simply excluded
        from the sum rather than failing the whole computation. This is a
        deliberate difference, not an oversight: "max"'s docstring warns
        that silently dropping a member can make MAX(remaining) look like a
        valid answer that quietly under-reports demand. A weighted average
        doesn't have that failure mode -- excluding one stale zone sensor
        from "RTU return air temp = weighted avg of zone temps by VAV
        airflow" still yields a physically meaningful average over the
        zones that ARE reporting, the same way a real BAS would drop a
        faulted sensor from a trend calculation rather than refuse to
        compute anything. A negative weight is never valid input (weights
        represent a physical quantity like airflow, which cannot be
        negative), so it is excluded the same way a missing value is,
        not clamped to zero and not treated as "the whole aggregate is
        unresolvable" the way "max" would.
        """
        numerator = 0.0
        denominator = 0.0
        pair_diagnostics: list[dict[str, Any]] = []
        for value_point_id, weight_point_id in zip(agg.point_ids, agg.weight_point_ids):
            value = _as_float(self._inputs.get(value_point_id)) if value_point_id in self._inputs else None
            weight = (
                _as_float(self._inputs.get(weight_point_id))
                if weight_point_id is not None and weight_point_id in self._inputs
                else None
            )
            used = value is not None and weight is not None and weight >= 0
            if used:
                numerator += value * weight
                denominator += weight
            pair_diagnostics.append({
                "value_point_id": value_point_id,
                "weight_point_id": weight_point_id,
                "value": value,
                "weight": weight,
                "used": used,
            })
        if denominator <= 0:
            return None, pair_diagnostics
        return numerator / denominator, pair_diagnostics

    def _weighted_average_failure_detail(
        self, agg: FMUAggregateInput, pair_diagnostics: list[dict[str, Any]],
    ) -> str:
        """Only called when _resolve_weighted_average returned None (total
        valid weight is 0) -- which happens two distinct ways: every pair
        was individually invalid (missing value, missing/negative weight),
        or every pair resolved fine but every resolved weight was exactly
        0 (e.g. every VAV's airflow reads 0 -- fan off). Both are reported
        per-pair so it's clear which case occurred, not just that the
        total is 0."""
        parts: list[str] = []
        for pair in pair_diagnostics:
            value_id = pair["value_point_id"]
            weight_id = pair["weight_point_id"]
            value_meta = self._aggregate_member_metadata(agg, value_id)
            label = f"{value_meta.get('device_name')}/{value_meta.get('point_name')}"
            if pair["used"]:
                parts.append(f"point {value_id} ({label}): value={pair['value']!r}, weight={pair['weight']!r}")
                continue
            reasons: list[str] = []
            if pair["value"] is None:
                reasons.append("missing/non-numeric value")
            if weight_id is None:
                reasons.append("no weight point configured")
            elif pair["weight"] is None:
                reasons.append("missing/non-numeric weight")
            elif pair["weight"] < 0:
                reasons.append(f"negative weight ({pair['weight']!r})")
            parts.append(f"point {value_id} ({label}): excluded ({', '.join(reasons)})")
        return (
            f"{agg.variable} <- aggregate(weighted_average) of "
            f"{list(agg.point_ids)}: total valid weight is 0: " + "; ".join(parts)
        )

    def _convert_output_value(self, binding: FMUPointBinding, value: Any) -> Any:
        if binding.variable != "supply_airflow_m3_s":
            return value
        metadata = self._binding_metadata(binding)
        units = str(metadata.get("units") or "").lower()
        normalized_units = "".join(ch for ch in units if ch.isalnum())
        if (
            normalized_units in {"cfm", "cubicfeetperminute"}
            or ("cubicfeet" in normalized_units and "minute" in normalized_units)
        ):
            try:
                return float(value) * 2118.880003
            except (TypeError, ValueError):
                return value
        return value

    def _build_step_payload(self, dt: float) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        payload: dict[str, Any] = {
            "session_id": self._session_id,
            "time_step": float(dt),
            "inputs": {},
        }
        inputs = payload["inputs"]
        input_report: dict[str, dict[str, Any]] = {}

        for variable, value in self._input_defaults.items():
            inputs[variable] = value
            input_report[variable] = {
                "source": "default",
                "value": value,
            }

        for binding in self._bindings:
            if binding.direction != "input":
                continue
            metadata = self._binding_metadata(binding)
            source_label = f"{metadata.get('device_name')}/{metadata.get('point_name')}"
            if binding.point_id not in self._inputs:
                input_report.setdefault(
                    binding.variable,
                    {
                        "source": "runtime-default",
                        "value": None,
                        "point_id": binding.point_id,
                        **metadata,
                    },
                )
                log.warning(
                    "FMU INPUT RESOLVE input=%s mode=point source=%s value=MISSING "
                    "device_id=%s object=%s:%s point_id=%s -- point has no live "
                    "value yet; runtime will apply its own metadata default "
                    "instead of the mapped point",
                    binding.variable,
                    source_label,
                    metadata.get("device_id"),
                    metadata.get("object_type"),
                    metadata.get("object_instance"),
                    binding.point_id,
                )
                continue
            value = self._inputs[binding.point_id]
            inputs[binding.variable] = value
            input_report[binding.variable] = {
                "source": "mapped-point",
                "value": value,
                "point_id": binding.point_id,
                **metadata,
            }
            log.info(
                "FMU INPUT RESOLVE input=%s mode=point source=%s value=%s",
                binding.variable,
                source_label,
                _fmt_resolve_value(value),
            )

        for agg in self._aggregate_inputs:
            aggregate_value, detail, diagnostics = self._resolve_one_aggregate(agg)
            if detail is not None:
                log.error(
                    "FMU AGGREGATE variable=%s operation=%s point_ids=%s -- "
                    "step cannot resolve every member, %s",
                    agg.variable,
                    agg.operation,
                    list(agg.point_ids),
                    detail,
                )
                # Never computed from a partial/unresolvable member set and
                # never left to fall through to the FMU runtime's default --
                # see FMUAggregateStepError (max) / _resolve_weighted_average
                # (weighted_average, which tolerates individual pair
                # failures but still refuses to divide by a zero total
                # weight). The whole step aborts here, before any HTTP call,
                # rather than sending a payload built from an incomplete/
                # untrustworthy input set.
                raise FMUAggregateStepError(detail)
            inputs[agg.variable] = aggregate_value
            input_report[agg.variable] = {
                "source": "aggregate",
                "operation": agg.operation,
                "point_ids": list(agg.point_ids),
                "value": aggregate_value,
                **diagnostics,
            }
            log.info(
                "FMU AGGREGATE variable=%s operation=%s aggregate=%s",
                agg.variable,
                agg.operation,
                aggregate_value,
            )

        return payload, input_report

    def step(self, dt: float) -> None:
        if self._status != ProviderStatus.RUNNING:
            return
        if dt <= 0:
            self._status = ProviderStatus.ERROR
            self._error = "dt must be greater than 0"
            return
        if not self._session_id:
            self._status = ProviderStatus.ERROR
            self._error = "FMU runtime session is not initialized"
            return

        try:
            payload, input_report = self._build_step_payload(dt)
        except FMUAggregateStepError as exc:
            self._status = ProviderStatus.ERROR
            self._error = str(exc)
            log.error(
                "FMU step aggregate resolution failed: provider=%s "
                "device_ids=%s model=%s session=%s error=%s",
                self._provider_id(),
                self._device_ids(),
                self._model,
                self._session_id,
                exc,
            )
            return

        output_bindings = self._output_bindings_by_variable()
        first_step = not self._first_step_logged
        status_before = self._status
        self._step_sequence += 1
        step_id = f"{self._provider_id() or self._model}:{self._step_sequence}"
        if self._step_in_progress:
            log.warning(
                "Concurrent BACnet FMU provider step detected: provider=%s "
                "device_ids=%s model=%s session=%s step=%s dt=%s inputs=%s",
                self._provider_id(),
                self._device_ids(),
                self._model,
                self._session_id,
                step_id,
                float(dt),
                input_report,
            )
        self._step_in_progress = True
        self._last_step_id = step_id

        try:
            response = self._client.step(self._model, payload)
            if response.status_code == 404:
                self._error = (
                    "FMU runtime session was lost, likely because the FMU "
                    "runtime container restarted. Suppressing outputs; the "
                    "simulation model must be reloaded/reset to start a new "
                    "trajectory."
                )
                self._last_http_status = response.status_code
                self._last_raw_response = response.raw_body
                self._outputs = {}
                self._session_id = None
                self._status = ProviderStatus.ERROR
                log.error(
                    "FMU runtime session lost; stopping provider: "
                    "provider=%s device_ids=%s model=%s session=%s step=%s "
                    "time_step=%s status_before=%s status_after=%s "
                    "inputs=%s raw_body=%s last_outputs=%s",
                    self._provider_id(),
                    self._device_ids(),
                    self._model,
                    self._session_id,
                    step_id,
                    float(dt),
                    status_before,
                    self._status,
                    input_report,
                    response.raw_body,
                    self._outputs,
                )
                return

            output_updates: dict[int, Any] = {}
            output_report: dict[str, dict[str, Any]] = {}
            missing_fields: list[str] = []
            self._last_http_status = response.status_code
            self._last_raw_response = response.raw_body

            if response.status_code >= 400:
                self._error = (
                    f"FMU runtime HTTP {response.status_code}: "
                    f"{response.raw_body[:500]}"
                )
                log.error(
                    "FMU step HTTP error: provider=%s device_ids=%s "
                    "session=%s model=%s step=%s "
                    "time_step=%s status_before=%s status_after=%s "
                    "http_status=%s inputs=%s raw_body=%s last_outputs=%s",
                    self._provider_id(),
                    self._device_ids(),
                    self._session_id,
                    self._model,
                    step_id,
                    float(dt),
                    status_before,
                    self._status,
                    response.status_code,
                    input_report,
                    response.raw_body,
                    self._outputs,
                )
                return

            result = response.body
            self._runtime_state = str(result.get("state") or self._runtime_state or "")
            self._warmup_progress = _as_float(result.get("warmup_progress"))
            self._warmup_seconds = _as_float(result.get("warmup_seconds"))
            if self._runtime_state and self._runtime_state != "RUNNING":
                self._outputs = {}
                self._error = (
                    f"FMU runtime session is {self._runtime_state}; "
                    f"warmup_progress={self._warmup_progress}"
                )
                log.info(
                    "FMU runtime not running; suppressing outputs: "
                    "provider=%s device_ids=%s session=%s model=%s step=%s "
                    "state=%s warmup_progress=%s raw_body=%s",
                    self._provider_id(),
                    self._device_ids(),
                    self._session_id,
                    self._model,
                    step_id,
                    self._runtime_state,
                    self._warmup_progress,
                    response.raw_body,
                )
                return
            runtime_time = result.get("current_time")
            for variable, binding in output_bindings.items():
                if variable not in result:
                    missing_fields.append(variable)
                    continue
                value = self._convert_output_value(binding, result[variable])
                output_updates[binding.point_id] = value
                output_report[variable] = {
                    "value": value,
                    "point_id": binding.point_id,
                    **self._binding_metadata(binding),
                }

            if runtime_time is not None:
                try:
                    numeric_runtime_time = float(runtime_time)
                except (TypeError, ValueError):
                    numeric_runtime_time = None
                if numeric_runtime_time is not None:
                    if (
                        self._last_runtime_time is not None
                        and numeric_runtime_time + 1e-9 < self._last_runtime_time
                    ):
                        log.warning(
                            "FMU runtime simulation time moved backward: "
                            "provider=%s device_ids=%s model=%s session=%s "
                            "step=%s previous_sim_time=%s current_sim_time=%s "
                            "dt=%s inputs=%s outputs=%s raw_body=%s",
                            self._provider_id(),
                            self._device_ids(),
                            self._model,
                            self._session_id,
                            step_id,
                            self._last_runtime_time,
                            numeric_runtime_time,
                            float(dt),
                            input_report,
                            output_report,
                            response.raw_body,
                        )
                    elif (
                        self._last_runtime_time is not None
                        and abs((numeric_runtime_time - self._last_runtime_time) - float(dt)) > max(1e-6, float(dt) * 0.25)
                    ):
                        log.warning(
                            "FMU runtime simulation time jump: provider=%s "
                            "device_ids=%s model=%s session=%s step=%s "
                            "previous_sim_time=%s current_sim_time=%s dt=%s "
                            "inputs=%s outputs=%s raw_body=%s",
                            self._provider_id(),
                            self._device_ids(),
                            self._model,
                            self._session_id,
                            step_id,
                            self._last_runtime_time,
                            numeric_runtime_time,
                            float(dt),
                            input_report,
                            output_report,
                            response.raw_body,
                        )
                    self._last_runtime_time = numeric_runtime_time

            if output_updates:
                self._outputs = {
                    **self._outputs,
                    **output_updates,
                }
                self._error = None
                self._last_step_outputs = output_report
                self._last_step_inputs = input_report
            else:
                self._error = (
                    "FMU runtime response did not include any configured "
                    f"output fields: {', '.join(sorted(output_bindings))}"
                )

            log.info(
                "FMU step: provider=%s device_ids=%s session=%s model=%s "
                "step=%s sim_time=%s time_step=%s status_before=%s "
                "status_after=%s http_status=%s inputs=%s raw_body=%s "
                "mapped_outputs=%s point_outputs=%s missing_output_fields=%s",
                self._provider_id(),
                self._device_ids(),
                self._session_id,
                self._model,
                step_id,
                runtime_time,
                float(dt),
                status_before,
                self._status,
                response.status_code,
                input_report,
                response.raw_body,
                output_report,
                self._outputs,
                missing_fields,
            )
            if first_step:
                self._first_step_logged = True
        except Exception as exc:
            self._error = str(exc)
            log.exception(
                "FMU step exception: provider=%s session=%s model=%s "
                "time_step=%s status_before=%s status_after=%s inputs=%s "
                "last_outputs=%s",
                self._provider_id(),
                self._session_id,
                self._model,
                float(dt),
                status_before,
                self._status,
                input_report,
                self._outputs,
            )
        finally:
            self._step_in_progress = False

    def set_inputs(self, values: Mapping[int, Any]) -> None:
        self._inputs.update(values)

    def get_outputs(self) -> Mapping[int, Any]:
        return dict(self._outputs)

    def validate(self) -> ValidationResult:
        errors: list[str] = []
        if not self._runtime_url:
            errors.append("FMU runtime URL is required")
        if not self._model:
            errors.append("FMU runtime model is required")

        for binding in self._bindings:
            if binding.direction not in {"input", "output"}:
                errors.append(f"Invalid direction {binding.direction!r} for point {binding.point_id}")
            elif self._input_variables and binding.direction == "input" and binding.variable not in self._input_variables:
                errors.append(f"Unsupported FMU input field: {binding.variable}")
            elif self._output_variables and binding.direction == "output" and binding.variable not in self._output_variables:
                errors.append(f"Unsupported FMU output field: {binding.variable}")

        for agg in self._aggregate_inputs:
            if not agg.point_ids:
                errors.append(f"Aggregate input {agg.variable!r} has no source points")
            if agg.operation not in _SUPPORTED_AGGREGATE_OPERATIONS:
                errors.append(
                    f"Aggregate input {agg.variable!r} has unsupported operation "
                    f"{agg.operation!r} (supported: {sorted(_SUPPORTED_AGGREGATE_OPERATIONS)})"
                )
            elif agg.operation == "weighted_average" and agg.point_ids:
                # Every value point must have exactly one paired weight
                # point -- see FMUAggregateInput's own docstring for why
                # weight_point_ids is positionally paired with point_ids.
                if len(agg.weight_point_ids) != len(agg.point_ids):
                    errors.append(
                        f"Aggregate input {agg.variable!r} (weighted_average) must have "
                        f"exactly one weight point per value point "
                        f"({len(agg.point_ids)} value point(s), "
                        f"{len(agg.weight_point_ids)} weight point(s))"
                    )
                else:
                    missing_weight_for = [
                        point_id
                        for point_id, weight_id in zip(agg.point_ids, agg.weight_point_ids)
                        if weight_id is None
                    ]
                    if missing_weight_for:
                        errors.append(
                            f"Aggregate input {agg.variable!r} (weighted_average) is missing a "
                            f"weight point for value point(s): {missing_weight_for}"
                        )
            if self._input_variables and agg.variable not in self._input_variables:
                errors.append(f"Unsupported FMU input field: {agg.variable}")

        # Defensive second layer -- _build_fmu_provider should already reject
        # this at registration time, but a provider can also be constructed
        # directly (as the tests do), so validate() re-checks that no
        # variable is targeted by more than one input source (Point+Point,
        # Point+Aggregate, or Aggregate+Aggregate all silently collide on
        # the same `inputs[variable] = ...` payload key otherwise).
        input_variable_counts: dict[str, int] = {}
        for binding in self._bindings:
            if binding.direction == "input":
                input_variable_counts[binding.variable] = input_variable_counts.get(binding.variable, 0) + 1
        for agg in self._aggregate_inputs:
            input_variable_counts[agg.variable] = input_variable_counts.get(agg.variable, 0) + 1
        for variable, count in input_variable_counts.items():
            if count > 1:
                errors.append(f"Input variable {variable!r} has {count} conflicting mappings")

        if self._error:
            errors.append(self._error)

        return ValidationResult(valid=not errors, errors=errors)

    def get_status(self) -> ProviderStatus:
        return self._status

    def get_diagnostics(self) -> dict[str, Any]:
        return {
            "session_id": self._session_id,
            "runtime_url": self._runtime_url,
            "model": self._model,
            "error": self._error,
            "last_outputs": dict(self._outputs),
            "last_runtime_time": self._last_runtime_time,
            "last_step_inputs": dict(self._last_step_inputs),
            "last_step_outputs": dict(self._last_step_outputs),
            "device_ids": self._device_ids(),
            "last_step_id": self._last_step_id,
            "last_http_status": self._last_http_status,
            "last_raw_response": self._last_raw_response,
            "step_in_progress": self._step_in_progress,
            "runtime_state": self._runtime_state,
            "warmup_progress": self._warmup_progress,
            "warmup_seconds": self._warmup_seconds,
        }

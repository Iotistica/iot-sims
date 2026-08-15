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

@dataclass(frozen=True)
class FMUPointBinding:
    point_id: int
    variable: str
    direction: str


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
        input_defaults: Mapping[str, Any] | None = None,
        timeout_s: float = 20.0,
        input_variables: set[str] | None = None,
        output_variables: set[str] | None = None,
    ) -> None:
        self._runtime_url = runtime_url.rstrip("/")
        self._model = model
        self._bindings = bindings
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
        self._inputs = {}
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
            health = self._client.health()
            if health.get("status") not in (None, "ok"):
                raise RuntimeError(f"FMU runtime health is {health.get('status')!r}")
            self._session_id = self._initialize_runtime_session(
                self._input_defaults,
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
                        "object_type",
                        "object_instance",
                        "units",
                    )
                    if item.get(key) is not None
                }
        return {}

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
            if binding.point_id not in self._inputs:
                input_report.setdefault(
                    binding.variable,
                    {
                        "source": "runtime-default",
                        "value": None,
                        "point_id": binding.point_id,
                        **self._binding_metadata(binding),
                    },
                )
                continue
            value = self._inputs[binding.point_id]
            inputs[binding.variable] = value
            input_report[binding.variable] = {
                "source": "mapped-point",
                "value": value,
                "point_id": binding.point_id,
                **self._binding_metadata(binding),
            }

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

        payload, input_report = self._build_step_payload(dt)
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

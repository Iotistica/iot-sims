from __future__ import annotations

import threading
import time
import uuid
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .catalog import ModelCatalog, ModelMetadata
from .conversions import apply_input_conversion, apply_output_conversion, scalar
from .diagnostics import build_step_diagnostics
from ..storage import RUNTIME_DATA_DIR


log = logging.getLogger("iot-models.runtime")


def load_fmu_model(fmu_path: str):
    from pyfmi import load_fmu

    # "auto" reads the FMU's own modelDescription.xml and picks whichever
    # kind it actually declares support for -- every catalog model to date
    # has only ever supported ME (plain Modelica-equation FMUs, no reason
    # for it to be otherwise), but EnergyPlusThermalZone.fmu is Spawn/
    # EnergyPlus-coupled and can only be exported as Co-Simulation (the
    # EnergyPlus binary does its own internal time-stepping; there's no
    # ODE for OMC to hand an external integrator). Every pyfmi call this
    # module makes on `fmu` (simulate/simulate_options/get_variable_start/
    # get_log/set) is part of FMUModelBase2's common interface, shared by
    # both FMUModelME2 and FMUModelCS2, so nothing else here needs to
    # change based on which kind gets loaded.
    return load_fmu(fmu_path, kind="auto")


def unique_result_path() -> Path:
    """A collision-proof, per-call result-file path for fmu.simulate() --
    see step()/_warm_up_session()'s own comments for why this exists and
    why result_handling="memory" (tried first) isn't a safe alternative.
    Caller is responsible for deleting the file once done reading from the
    returned `result` object (pyfmi's file-backed result may read lazily,
    so delete only after the last result[...] access, never sooner)."""
    results_dir = RUNTIME_DATA_DIR / "fmu-results"
    results_dir.mkdir(parents=True, exist_ok=True)
    return results_dir / f"{uuid.uuid4().hex}.mat"


class SessionState(str, Enum):
    INITIALIZING = "INITIALIZING"
    WARMING_UP = "WARMING_UP"
    RUNNING = "RUNNING"
    FAILED = "FAILED"
    TERMINATED = "TERMINATED"


@dataclass
class RuntimeSession:
    id: str
    model_id: str
    state: SessionState = SessionState.INITIALIZING
    current_time: float = 0.0
    warmup_seconds: float = 300.0
    warmup_completed_seconds: float = 0.0
    input_history: list[tuple[float, list[float]]] = field(default_factory=list)
    last_accessed: float = field(default_factory=time.time)
    latest_api_inputs: dict[str, Any] = field(default_factory=dict)
    latest_fmu_inputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    latest_outputs: dict[str, Any] = field(default_factory=dict)
    latest_output_deltas: dict[str, float] = field(default_factory=dict)
    latest_diagnostics: dict[str, Any] = field(default_factory=dict)
    latest_solver: dict[str, Any] = field(default_factory=dict)
    # FMI causality="parameter" String overrides resolved once at
    # initialize() (e.g. epwName/weaName) -- reapplied on every fresh
    # FMU reload (see _apply_string_parameters), since a `fixed` parameter
    # set once does not persist across the reinstantiation _warm_up_session/
    # step already do on every call.
    string_parameters: dict[str, str] = field(default_factory=dict)
    step_sequence: int = 0
    step_in_progress: bool = False
    active_step_id: str | None = None

    @property
    def warmup_progress(self) -> float:
        if self.warmup_seconds <= 0:
            return 1.0
        return max(0.0, min(1.0, self.warmup_completed_seconds / self.warmup_seconds))


class FMURuntimeError(RuntimeError):
    pass


class FMUSessionManager:
    def __init__(self, catalog: ModelCatalog) -> None:
        self.catalog = catalog
        self._sessions: dict[str, RuntimeSession] = {}
        self._lock = threading.Lock()

    @property
    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)

    def session_model_id(self, session_id: str) -> str:
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError("Session not found")
            return self._sessions[session_id].model_id

    def initialize(
        self,
        model_id: str,
        inputs: dict[str, Any] | None = None,
        string_parameters: dict[str, str] | None = None,
        warmup_seconds: float | None = None,
    ) -> dict[str, Any]:
        model = self.catalog.get(model_id)
        if not model.fmu_path.exists():
            log.error(
                "FMU initialize failed: model=%s fmu_path=%s reason=missing_fmu",
                model.id,
                model.fmu_path,
            )
            raise FMURuntimeError(f"FMU not found: {model.fmu_path}")

        api_inputs, fmu_input_report, values = self._make_fmu_input_payload(
            model,
            inputs or {},
        )
        resolved_string_parameters = self._make_string_parameter_payload(
            model,
            string_parameters or {},
        )
        resolved_warmup_seconds = max(
            0.0,
            float(warmup_seconds if warmup_seconds is not None else model.warmup_seconds),
        )
        session = RuntimeSession(
            id=str(uuid.uuid4()),
            model_id=model.id,
            warmup_seconds=resolved_warmup_seconds,
            input_history=[(0.0, values)],
            latest_api_inputs=api_inputs,
            latest_fmu_inputs=fmu_input_report,
            string_parameters=resolved_string_parameters,
        )
        with self._lock:
            self._sessions[session.id] = session
        log.info(
            "FMU session initializing: model=%s session=%s fmu_path=%s "
            "warmup_seconds=%s inputs=%s string_parameters=%s",
            model.id,
            session.id,
            model.fmu_path,
            session.warmup_seconds,
            sorted(api_inputs),
            resolved_string_parameters,
        )
        try:
            self._warm_up_session(session.id)
        except Exception:
            with self._lock:
                if session.id in self._sessions:
                    self._sessions[session.id].state = SessionState.FAILED
            raise

        with self._lock:
            session = self._sessions[session.id]
            return self._session_response(session)

    def step(self, session_id: str, time_step: float, inputs: dict[str, Any]) -> dict[str, Any]:
        if time_step <= 0:
            raise ValueError("time_step must be > 0")

        with self._lock:
            if session_id not in self._sessions:
                raise KeyError("Session not found")
            session = self._sessions[session_id]
            if session.state != SessionState.RUNNING:
                raise FMURuntimeError(
                    f"Session {session_id} is {session.state}; warmup_progress={session.warmup_progress:.2f}"
                )
            session.step_sequence += 1
            step_id = f"{session.id}:{session.step_sequence}"
            concurrent_step_id = session.active_step_id
            if session.step_in_progress:
                log.warning(
                    "Concurrent FMU step detected: model=%s session=%s "
                    "active_step=%s new_step=%s current_time=%s requested_dt=%s",
                    session.model_id,
                    session.id,
                    concurrent_step_id,
                    step_id,
                    session.current_time,
                    time_step,
                )
            session.step_in_progress = True
            session.active_step_id = step_id

        model = self.catalog.get(session.model_id)
        start_time = session.current_time
        final_time = start_time + time_step
        api_inputs, fmu_input_report, values = self._make_fmu_input_payload(model, inputs)
        history = list(session.input_history)
        if history and abs(history[-1][0] - start_time) < 1e-9:
            history[-1] = (start_time, values)
        else:
            history.append((start_time, values))

        fmu = None
        elapsed_ms = 0.0
        solver_log_tail = ""
        result_path: Path | None = None
        try:
            fmu = load_fmu_model(str(model.fmu_path))
            log.info(
                "FMU model loaded: model=%s label=%s session=%s step=%s "
                "fmu_path=%s inputs=%s outputs=%s",
                model.id,
                model.label,
                session_id,
                step_id,
                model.fmu_path,
                len(model.inputs),
                len(model.outputs),
            )
            self._apply_string_parameters(fmu, session.string_parameters, model)
            real_indices, discrete_indices = self._split_input_variables(fmu, model)
            if discrete_indices:
                self._apply_discrete_inputs(fmu, model, discrete_indices, values)
            trajectory = self._build_input_trajectory(history, final_time, real_indices)
            real_names = [model.inputs[i].fmu_variable for i in real_indices]
            options = fmu.simulate_options()
            options["ncp"] = max(1, int(final_time / 60.0))
            # Every step reinstantiates and re-simulates the FMU from
            # scratch (see this module's docstring/history-replay design),
            # and pyfmi's default result_handling writes a .mat file named
            # only after the model (e.g. "Weather_result.mat") -- with
            # multiple concurrent sessions of the *same* model (two Weather
            # configs stepping around the same tick), two simulate() calls
            # can collide on that one shared filename, and pyfmi raises
            # "The result file has been modified since the result object
            # was created." A per-call unique result_file_name sidesteps
            # the collision without touching result_handling at all --
            # result_handling="memory" was tried first (avoids the disk
            # write entirely) but confirmed, empirically, to silently
            # corrupt output values for any model with discrete/state-event
            # behavior (LightingZone's occupancy/manual-override `when`
            # blocks reproduced this: algebraic outputs pinned at a stale
            # value while values computed *from* them downstream, read
            # differently, stayed correct -- Weather/EnergyPlusThermalZone
            # never surfaced this because neither has Modelica `when`
            # blocks of its own). Deleted in `finally` below once every
            # result[...] read is done, so no .mat file accumulates.
            result_path = unique_result_path()
            options["result_file_name"] = str(result_path)
            started = time.perf_counter()
            result = fmu.simulate(
                start_time=0.0,
                final_time=final_time,
                input=(real_names, trajectory),
                options=options,
            )
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            solver_log_tail = self._fmu_log_tail(fmu, max_lines=20)
            outputs, raw_outputs = self._read_outputs(model, result)
            log.debug(
                "FMU step raw outputs: model=%s session=%s step=%s "
                "raw_outputs=%s converted_outputs=%s",
                model.id,
                session_id,
                step_id,
                raw_outputs,
                outputs,
            )
        except Exception as exc:
            detail = str(exc)
            if fmu is not None:
                log_tail = self._fmu_log_tail(fmu)
                if log_tail:
                    detail += "\n\nFMU log:\n" + log_tail
            log.error(
                "FMU step failed: model=%s session=%s step=%s t=%s->%s "
                "dt=%s error=%s",
                model.id,
                session_id,
                step_id,
                start_time,
                final_time,
                time_step,
                detail,
            )
            with self._lock:
                if session.active_step_id == step_id:
                    session.step_in_progress = False
                    session.active_step_id = None
            raise FMURuntimeError(detail) from exc
        finally:
            if result_path is not None:
                result_path.unlink(missing_ok=True)

        previous_outputs = dict(session.latest_outputs)
        debug_state = self._safe_build_diagnostics(
            model=model,
            session_id=session_id,
            start_time=start_time,
            final_time=final_time,
            time_step=time_step,
            api_inputs=api_inputs,
            fmu_inputs=fmu_input_report,
            outputs=outputs,
            previous_outputs=previous_outputs,
            elapsed_ms=elapsed_ms,
            solver_log_tail=solver_log_tail,
        )
        self._log_step(debug_state)

        with self._lock:
            session.current_time = final_time
            session.input_history = history
            session.last_accessed = time.time()
            session.latest_api_inputs = api_inputs
            session.latest_fmu_inputs = fmu_input_report
            session.latest_outputs = outputs
            session.latest_output_deltas = debug_state.get("output_deltas", {})
            session.latest_diagnostics = debug_state.get("diagnostics", {})
            session.latest_solver = debug_state.get("solver", {})
            if session.active_step_id == step_id:
                session.step_in_progress = False
                session.active_step_id = None

        log.info(
            "FMU step response: model=%s session=%s step=%s t=%s->%s dt=%s "
            "outputs=%s",
            model.id,
            session_id,
            step_id,
            start_time,
            final_time,
            time_step,
            outputs,
        )
        return {
            **self._session_response(session),
            **outputs,
        }

    def terminate(self, session_id: str) -> None:
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError("Session not found")
            session = self._sessions[session_id]
            model_id = session.model_id
            session.state = SessionState.TERMINATED
            del self._sessions[session_id]
        log.info("FMU session terminated: model=%s session=%s", model_id, session_id)

    def get_session_state(self, model_id: str, session_id: str) -> dict[str, Any]:
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError("Session not found")
            session = self._sessions[session_id]
            if session.model_id != model_id:
                raise KeyError("Session model mismatch")
            return {
                "model_id": session.model_id,
                "session_id": session.id,
                "state": session.state.value,
                "current_time": session.current_time,
                "warmup_seconds": session.warmup_seconds,
                "warmup_completed_seconds": session.warmup_completed_seconds,
                "warmup_progress": session.warmup_progress,
                "api_inputs": dict(session.latest_api_inputs),
                "fmu_inputs": dict(session.latest_fmu_inputs),
                "string_parameters": dict(session.string_parameters),
                "outputs": dict(session.latest_outputs),
                "output_deltas": dict(session.latest_output_deltas),
                "diagnostics": dict(session.latest_diagnostics),
                "solver": dict(session.latest_solver),
                "last_accessed": session.last_accessed,
            }

    def cleanup_inactive_sessions(self, max_idle_seconds: int = 1800) -> None:
        now = time.time()
        with self._lock:
            expired = [
                session_id
                for session_id, session in self._sessions.items()
                if now - session.last_accessed > max_idle_seconds
            ]
            for session_id in expired:
                del self._sessions[session_id]

    def _session_response(self, session: RuntimeSession) -> dict[str, Any]:
        return {
            "session_id": session.id,
            "model_id": session.model_id,
            "state": session.state.value,
            "current_time": session.current_time,
            "warmup_seconds": session.warmup_seconds,
            "warmup_completed_seconds": session.warmup_completed_seconds,
            "warmup_progress": session.warmup_progress,
        }

    def _warm_up_session(self, session_id: str) -> None:
        with self._lock:
            if session_id not in self._sessions:
                raise KeyError("Session not found")
            session = self._sessions[session_id]
            model = self.catalog.get(session.model_id)
            warmup_seconds = max(0.0, session.warmup_seconds)
            if warmup_seconds <= 0:
                session.state = SessionState.RUNNING
                session.warmup_completed_seconds = 0.0
                log.info(
                    "FMU warmup skipped: model=%s session=%s warmup_seconds=0",
                    model.id,
                    session_id,
                )
                return
            session.state = SessionState.WARMING_UP

        log.info(
            "FMU warmup start: model=%s session=%s warmup_seconds=%s",
            model.id,
            session_id,
            warmup_seconds,
        )

        fmu = None
        started = time.perf_counter()
        result_path: Path | None = None
        try:
            fmu = load_fmu_model(str(model.fmu_path))
            # Set before anything else touches the FMU (even the read-only
            # get_variable_start diagnostic below is safe on its own, but
            # ordering string-parameter sets first keeps this unambiguously
            # correct without relying on that). FMI2 only allows
            # fmi2SetString on a `fixed` parameter before
            # fmi2EnterInitializationMode, i.e. right after instantiation --
            # see _apply_string_parameters's own docstring for why this must
            # never be preceded by a live fmu.get() call.
            self._apply_string_parameters(fmu, session.string_parameters, model)
            # Startup diagnostic only -- read-only, never written back to the
            # FMU. Answers "what start/initial-condition parameter value did
            # the FMU actually instantiate with" independent of whether that
            # parameter is exposed through model.json's declared inputs (it
            # currently is NOT for e.g. ThermalZone.mo's TRoo_start -- see
            # the "FMU STARTUP DIAGNOSTICS" log line below). Wrapped
            # defensively since most models have no such variable at all.
            #
            # Uses get_variable_start() (reads the value declared in
            # modelDescription.xml) rather than get() (a live fmi2GetReal
            # call) deliberately: at this point the FMU instance is freshly
            # instantiated but has not yet entered initialization mode, and
            # fmi2GetReal is illegal in that state for FMI2 Model Exchange.
            # A live get() here doesn't just fail for this one value -- FMIL
            # transitions the WHOLE instance to its error state on the
            # illegal call, so every later call (including the real
            # fmi2SetReal the warmup simulate() below depends on) then fails
            # too. Confirmed by reproducing exactly this failure mode
            # against a real compiled FMU before landing this fix.
            start_parameter_values: dict[str, float] = {}
            for candidate in ("TRoo_start",):
                try:
                    start_parameter_values[candidate] = scalar(fmu.get_variable_start(candidate))
                except Exception:
                    pass
            real_indices, discrete_indices = self._split_input_variables(fmu, model)
            if discrete_indices and session.input_history:
                self._apply_discrete_inputs(
                    fmu, model, discrete_indices, session.input_history[-1][1]
                )
            trajectory = self._build_input_trajectory(
                list(session.input_history),
                warmup_seconds,
                real_indices,
            )
            real_names = [model.inputs[i].fmu_variable for i in real_indices]
            options = fmu.simulate_options()
            options["ncp"] = max(1, int(warmup_seconds / 60.0))
            # See the matching comment in step() -- a unique per-call
            # result_file_name avoids concurrent same-model sessions
            # colliding on pyfmi's default model-named .mat file, without
            # result_handling="memory"'s confirmed output-corruption risk
            # for models with discrete/state-event behavior.
            result_path = unique_result_path()
            options["result_file_name"] = str(result_path)
            result = fmu.simulate(
                start_time=0.0,
                final_time=warmup_seconds,
                input=(real_names, trajectory),
                options=options,
            )
            warmup_outputs, raw_warmup_outputs = self._read_outputs(model, result)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            log.info(
                "FMU STARTUP DIAGNOSTICS: model=%s session=%s "
                "start_parameters=%s raw_outputs_after_warmup=%s "
                "converted_outputs_after_warmup=%s",
                model.id,
                session_id,
                start_parameter_values,
                raw_warmup_outputs,
                warmup_outputs,
            )
        except Exception as exc:
            detail = str(exc)
            if fmu is not None:
                log_tail = self._fmu_log_tail(fmu)
                if log_tail:
                    detail += "\n\nFMU log:\n" + log_tail
            with self._lock:
                if session_id in self._sessions:
                    self._sessions[session_id].state = SessionState.FAILED
            log.error(
                "FMU warmup failed: model=%s session=%s warmup_seconds=%s error=%s",
                model.id,
                session_id,
                warmup_seconds,
                detail,
            )
            raise FMURuntimeError(detail) from exc
        finally:
            if result_path is not None:
                result_path.unlink(missing_ok=True)

        with self._lock:
            session = self._sessions[session_id]
            session.current_time = warmup_seconds
            session.warmup_completed_seconds = warmup_seconds
            session.input_history = [
                *list(session.input_history),
                (warmup_seconds, list(session.input_history[-1][1])),
            ]
            session.last_accessed = time.time()
            session.state = SessionState.RUNNING

        log.info(
            "FMU warmup complete: model=%s session=%s sim_time=%s "
            "elapsed_ms=%.1f warmup_outputs_suppressed=%s",
            model.id,
            session_id,
            warmup_seconds,
            elapsed_ms,
            sorted(warmup_outputs),
        )

    @staticmethod
    def _build_input_trajectory(
        history: list[tuple[float, list[float]]],
        final_time: float,
        column_indices: list[int],
    ):
        rows = []
        for timestamp, values in history:
            if timestamp <= final_time:
                rows.append([timestamp, *(values[i] for i in column_indices)])
        if not rows:
            raise FMURuntimeError("FMU input history is empty")
        if rows[0][0] > 0:
            rows.insert(0, [0.0, *rows[0][1:]])
        if rows[-1][0] < final_time:
            rows.append([final_time, *rows[-1][1:]])
        try:
            import numpy as np

            return np.array(rows, dtype=float)
        except ImportError:
            return rows

    @staticmethod
    def _fmu_variable_is_real(fmu: Any, fmu_variable: str) -> bool:
        try:
            from pyfmi.fmi import FMI2_REAL
        except Exception:
            return True
        try:
            return fmu.get_variable_data_type(fmu_variable) == FMI2_REAL
        except Exception:
            return True

    @staticmethod
    def _fmu_variable_is_integer(fmu: Any, fmu_variable: str) -> bool:
        """Distinguishes an FMI2 Integer discrete input (e.g. LightingZone's
        manualMode, 0-3) from a Boolean one -- _apply_discrete_inputs must
        cast to int(), not bool(), for these or every value other than 0
        collapses to the same 1 (e.g. manualMode=2 and =3 both becoming
        1/ManualOff)."""
        try:
            from pyfmi.fmi import FMI2_INTEGER
        except Exception:
            return False
        try:
            return fmu.get_variable_data_type(fmu_variable) == FMI2_INTEGER
        except Exception:
            return False

    @classmethod
    def _split_input_variables(
        cls,
        fmu: Any,
        model: ModelMetadata,
    ) -> tuple[list[int], list[int]]:
        """Partition model.inputs into FMI2 Real vs discrete (Boolean/Integer)
        column indices. FMI2 forbids driving non-Real variables through the
        continuous `input=` trajectory once the model has entered
        continuous-time mode (fmi2SetBoolean is only legal before that), so
        discrete inputs must instead be applied with fmu.set() before
        simulate() and held constant for the step.
        """
        real_indices: list[int] = []
        discrete_indices: list[int] = []
        for index, item in enumerate(model.inputs):
            if cls._fmu_variable_is_real(fmu, item.fmu_variable):
                real_indices.append(index)
            else:
                discrete_indices.append(index)
        return real_indices, discrete_indices

    @classmethod
    def _apply_discrete_inputs(
        cls,
        fmu: Any,
        model: ModelMetadata,
        discrete_indices: list[int],
        values: list[float],
    ) -> None:
        for index in discrete_indices:
            item = model.inputs[index]
            if cls._fmu_variable_is_integer(fmu, item.fmu_variable):
                fmu.set(item.fmu_variable, int(round(values[index])))
            else:
                fmu.set(item.fmu_variable, bool(values[index]))

    @staticmethod
    def _make_string_parameter_payload(
        model: ModelMetadata,
        provided: dict[str, str],
    ) -> dict[str, str]:
        """Resolves each of model.string_parameters from the caller's value
        or the catalog default, raising on a missing required one. Mirrors
        _make_fmu_input_payload's shape/role but for FMI String parameters
        rather than Real inputs -- kept separate since the two have
        different FMI-level set semantics (see _apply_string_parameters)."""
        resolved: dict[str, str] = {}
        for item in model.string_parameters:
            value = provided.get(item.name, item.default)
            if value is None and item.required:
                raise ValueError(f"Missing required string parameter: {item.name}")
            if value is None:
                continue
            resolved[item.name] = str(value)
        return resolved

    @staticmethod
    def _apply_string_parameters(
        fmu: Any,
        string_parameters: dict[str, str],
        model: ModelMetadata,
    ) -> None:
        """Sets each resolved string parameter via a plain fmu.set() --
        confirmed (against a real compiled FMU, not just inferred) to
        dispatch to fmi2SetString correctly, the same way _apply_discrete_
        inputs's plain fmu.set(..., bool(...)) already dispatches to
        fmi2SetBoolean for discrete inputs.

        Must be called immediately after load_fmu_model(), before ANY
        fmu.get()/live FMI read call. Confirmed empirically: an FMI2 String
        `fixed` parameter can only be set in the Instantiated state, and a
        live fmu.get() call issued in that same state (as opposed to the
        metadata-only get_variable_start(), which is always safe) trips the
        FMU into its error state -- every subsequent call then fails too,
        including a `.set()` that would otherwise have succeeded. This
        mirrors the exact failure mode _warm_up_session's own
        get_variable_start comment already documents for a live fmu.get()
        on TRoo_start; the fix here is the same discipline: set first,
        never probe first.

        No-ops (leaves the FMU's own compiled-in default value in place)
        for any string_parameters entry not present in `string_parameters`
        -- e.g. a model with no Location-resolved weather override falls
        back to its own baked-in weaName/epwName untouched."""
        by_name = {item.name: item for item in model.string_parameters}
        for name, value in string_parameters.items():
            item = by_name.get(name)
            if item is None:
                continue
            fmu.set(item.fmu_variable, value)

    @staticmethod
    def _fmu_log_tail(model: Any, max_lines: int = 30) -> str:
        try:
            lines = [str(line) for line in model.get_log()]
        except Exception:
            return ""
        return "\n".join(lines[-max_lines:])

    @staticmethod
    def _make_fmu_input_payload(
        model: ModelMetadata,
        provided: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[float]]:
        api_inputs: dict[str, Any] = {}
        fmu_inputs: dict[str, dict[str, Any]] = {}
        values = []
        for item in model.inputs:
            value = provided.get(item.name, item.default)
            if value is None and item.required:
                raise ValueError(f"Missing required input: {item.name}")
            if value is None:
                value = 0.0
            numeric_value = float(value)
            converted = apply_input_conversion(numeric_value, item.conversion)
            api_inputs[item.name] = numeric_value
            fmu_inputs[item.name] = {
                "value": converted,
                "api_value": numeric_value,
                "api_unit": item.unit,
                "fmu_variable": item.fmu_variable,
                "fmu_unit": "K" if item.conversion == "c_to_k" else (
                    "fraction" if item.conversion == "pct_to_fraction" else item.unit
                ),
                "conversion": item.conversion,
            }
            values.append(converted)
        return api_inputs, fmu_inputs, values

    @staticmethod
    def _read_outputs(model: ModelMetadata, result: Any) -> tuple[dict[str, float], dict[str, float]]:
        """Returns (converted_outputs, raw_outputs) -- raw_outputs is the
        value straight off the FMU's own trajectory result, BEFORE
        apply_output_conversion (e.g. TRoo in Kelvin, before the k_to_c
        subtraction that produces the API-facing zone_temp_c). Both dicts
        are keyed by the API-facing variable name (item.name), not the FMU's
        internal fmu_variable, so a caller can line up
        raw_outputs["zone_temp_c"] against converted_outputs["zone_temp_c"]
        without needing model.outputs to look up the fmu_variable itself.
        Exists so startup/step diagnostics can show the pre- and post-
        conversion value side by side -- see _warm_up_session's "FMU
        STARTUP DIAGNOSTICS" log line."""
        outputs: dict[str, float] = {}
        raw_outputs: dict[str, float] = {}
        for item in model.outputs:
            value = scalar(result[item.fmu_variable])
            raw_outputs[item.name] = value
            outputs[item.name] = apply_output_conversion(value, item.conversion)
        return outputs, raw_outputs

    @staticmethod
    def _safe_build_diagnostics(**kwargs: Any) -> dict[str, Any]:
        try:
            return build_step_diagnostics(**kwargs)
        except Exception:
            log.exception("FMU diagnostics collection failed")
            model = kwargs.get("model")
            return {
                "model_id": getattr(model, "id", None),
                "session_id": kwargs.get("session_id"),
                "current_time": kwargs.get("start_time"),
                "target_time": kwargs.get("final_time"),
                "time_step": kwargs.get("time_step"),
                "api_inputs": kwargs.get("api_inputs", {}),
                "fmu_inputs": kwargs.get("fmu_inputs", {}),
                "outputs": kwargs.get("outputs", {}),
                "output_deltas": {},
                "diagnostics": {"warnings": ["diagnostics_failed"]},
                "solver": {
                    "elapsed_ms": kwargs.get("elapsed_ms"),
                    "log_tail": kwargs.get("solver_log_tail", ""),
                },
                "log_lines": {},
            }

    @staticmethod
    def _log_step(state: dict[str, Any]) -> None:
        lines = state.get("log_lines") or {}
        if lines:
            log.info(lines.get("step", "FMU step"))
            log.info(lines.get("api_inputs", ""))
            log.info(lines.get("fmu_inputs", ""))
            log.info(lines.get("outputs", ""))
            log.info(lines.get("diagnostics", ""))
            log.debug("OUTPUT deltas: %s", state.get("output_deltas") or {})
        warnings = (state.get("diagnostics") or {}).get("warnings") or []
        if warnings:
            log.warning(
                "FMU diagnostic warnings: model=%s session=%s warnings=%s",
                state.get("model_id"),
                state.get("session_id"),
                ",".join(str(item) for item in warnings),
            )

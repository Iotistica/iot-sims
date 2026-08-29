from __future__ import annotations

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ..errors import http_error
from ..state import catalog, manager

router = APIRouter()


class InitializeRequest(BaseModel):
    inputs: dict[str, float] = Field(default_factory=dict)
    # Session-lifetime FMI causality="parameter" String overrides (e.g.
    # EnergyPlusThermalZone's epwName/weaName, Weather's weaName) -- kept
    # separate from `inputs` rather than widening that dict[str, float] to
    # dict[str, Any], which would silently defeat the numeric validation
    # _make_fmu_input_payload's float(value) cast currently relies on.
    # Captured once here, not resent on StepRequest -- see manager.py's
    # RuntimeSession.string_parameters and _apply_string_parameters.
    string_parameters: dict[str, str] = Field(default_factory=dict)
    # Per-session override of the model's own static warmup_seconds (e.g.
    # Weather's "Playback Start Month" -- fast-forwards the session's clock
    # through that many seconds of the (wrapping) weather table before it
    # goes live, landing on the chosen calendar point). None (the default)
    # means "use model.warmup_seconds as-is", identical to today's
    # behavior for every model that doesn't send this. Generic -- iot-models
    # has no idea this is used for weather specifically; any model could use
    # a per-session warmup override for its own reasons.
    warmup_seconds: float | None = None


class InitializeResponse(BaseModel):
    session_id: str
    model_id: str
    state: str = "RUNNING"
    current_time: float = 0.0
    warmup_seconds: float = 0.0
    warmup_completed_seconds: float = 0.0
    warmup_progress: float = 1.0


class StepRequest(BaseModel):
    session_id: str
    time_step: float = 60.0
    inputs: dict[str, float] = Field(default_factory=dict)


@router.get("/models")
def list_models():
    return {"models": catalog.list_models()}


@router.get("/models/{model_id}/metadata")
def model_metadata(model_id: str):
    try:
        return catalog.get(model_id).to_dict()
    except Exception as exc:
        raise http_error(exc)


@router.post("/models/{model_id}/initialize", response_model=InitializeResponse)
def initialize_model(model_id: str, request: InitializeRequest | None = None):
    try:
        req = request or InitializeRequest()
        return manager.initialize(
            model_id, req.inputs, req.string_parameters, req.warmup_seconds,
        )
    except Exception as exc:
        raise http_error(exc)


@router.post("/models/{model_id}/step")
def step_model(model_id: str, request: StepRequest):
    try:
        catalog.get(model_id)
        if manager.session_model_id(request.session_id) != model_id:
            raise KeyError("Session model mismatch")
        result = manager.step(request.session_id, request.time_step, request.inputs)
        return result
    except Exception as exc:
        raise http_error(exc)


@router.post("/models/{model_id}/terminate")
def terminate_model(model_id: str, session_id: str = Query(...)):
    try:
        catalog.get(model_id)
        manager.terminate(session_id)
        return {"message": "Session successfully terminated"}
    except Exception as exc:
        raise http_error(exc)


@router.get("/models/{model_id}/sessions/{session_id}/state")
def session_state(model_id: str, session_id: str):
    try:
        catalog.get(model_id)
        return manager.get_session_state(model_id, session_id)
    except Exception as exc:
        raise http_error(exc)

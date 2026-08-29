from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class VariableMetadata:
    name: str
    fmu_variable: str
    unit: str | None = None
    conversion: str | None = None
    default: float | None = None
    required: bool = False
    label: str | None = None
    suggested_point_types: tuple[str, ...] = ()
    semantic: dict[str, Any] | None = None
    mapping_hints: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VariableMetadata":
        return cls(
            name=data["name"],
            fmu_variable=data["fmu_variable"],
            unit=data.get("unit"),
            conversion=data.get("conversion"),
            default=data.get("default"),
            required=bool(data.get("required", False)),
            label=data.get("label"),
            suggested_point_types=tuple(data.get("suggested_point_types") or ()),
            semantic=data.get("semantic"),
            mapping_hints=data.get("mapping_hints"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label or self.name,
            "fmu_variable": self.fmu_variable,
            "unit": self.unit,
            "conversion": self.conversion,
            "default": self.default,
            "required": self.required,
            "suggested_point_types": list(self.suggested_point_types),
            "semantic": self.semantic or {},
            "mapping_hints": self.mapping_hints or {},
        }


@dataclass(frozen=True)
class StringParameterMetadata:
    """A FMI causality="parameter" String variable (e.g. EnergyPlusThermalZone's
    epwName/weaName, or Weather's own weaName) that a caller can set at
    session initialize() time -- distinct from VariableMetadata, which
    assumes a plain float input/output. See manager.py's
    _apply_string_parameters for how these actually reach the FMU."""
    name: str
    fmu_variable: str
    default: str | None = None
    required: bool = False
    label: str | None = None
    # True for a parameter whose value is a file path (e.g. Weather's
    # wea_filename) -- tells the caller's UI to render an upload control
    # (via the generic /resources endpoint, see shared/runtime/resources/)
    # rather than a plain free-text box. False for any future string
    # parameter that's just a plain value, not a file reference.
    is_file: bool = False
    # True to tuck this parameter into the caller UI's "Advanced" section
    # instead of the common Parameters list -- e.g. EnergyPlusThermalZone's
    # wea_filename, which a bacnet-simulator upload to its epw_filename
    # sibling already auto-derives (see SimulationModelDrawer.vue's
    # sibling-autofill), so it needs no separate visible upload control in
    # the common case.
    advanced: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StringParameterMetadata":
        return cls(
            name=data["name"],
            fmu_variable=data["fmu_variable"],
            default=data.get("default"),
            required=bool(data.get("required", False)),
            label=data.get("label"),
            is_file=bool(data.get("is_file", False)),
            advanced=bool(data.get("advanced", False)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label or self.name,
            "fmu_variable": self.fmu_variable,
            "default": self.default,
            "required": self.required,
            "is_file": self.is_file,
            "advanced": self.advanced,
        }


@dataclass(frozen=True)
class ModelMetadata:
    id: str
    label: str
    fmu_path: Path
    inputs: list[VariableMetadata]
    outputs: list[VariableMetadata]
    description: str | None = None
    warmup_seconds: float = 300.0
    string_parameters: list[StringParameterMetadata] = field(default_factory=list)
    # Raw model.json "calibration" block (goal + tuners), passed through
    # verbatim -- nothing here parses/validates it beyond "is it present and
    # does it say enabled: true". run_hebo_calibration.py (invoked by
    # HEBOCalibrationRunner) is the actual consumer of goal/tuners; this
    # field only exists so a caller (e.g. bacnet-simulator's calibration
    # glue) can discover which output is the calibration goal and whether a
    # model supports calibration at all, without duplicating model.json
    # parsing.
    calibration: dict[str, Any] | None = None
    # Stable, human-readable identifier (e.g. "RTU", "SimpleVAVZone") --
    # `id` became an opaque GUID in the model-id migration; `slug` is what
    # code that needs a fixed, known-in-advance name should match against
    # (e.g. diagnostics.py's per-model branching), since a GUID can't be
    # hardcoded/predicted the way the old id string could. Falls back to
    # `id` itself for any model.json that predates this field.
    slug: str = ""

    @classmethod
    def from_file(cls, path: Path) -> "ModelMetadata":
        data = json.loads(path.read_text(encoding="utf-8"))
        fmu_path = Path(data["fmu_path"])
        if not fmu_path.is_absolute():
            fmu_path = path.parent / fmu_path
        return cls(
            id=data["id"],
            slug=str(data.get("slug") or data["id"]),
            label=data["label"],
            description=data.get("description"),
            fmu_path=fmu_path,
            inputs=[VariableMetadata.from_dict(item) for item in data.get("inputs", [])],
            outputs=[VariableMetadata.from_dict(item) for item in data.get("outputs", [])],
            warmup_seconds=float(data.get("warmup_seconds", 300.0)),
            string_parameters=[
                StringParameterMetadata.from_dict(item)
                for item in data.get("string_parameters", [])
            ],
            calibration=data.get("calibration"),
        )

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "slug": self.slug,
            "label": self.label,
            "description": self.description,
            "fmu_path": str(self.fmu_path),
            "fmu_exists": self.fmu_path.exists(),
            "warmup_seconds": self.warmup_seconds,
            "inputs": len(self.inputs),
            "outputs": len(self.outputs),
            "string_parameters": len(self.string_parameters),
            # Cheap boolean (not the full calibration block -- see to_dict)
            # so a model picker can filter/gray out non-calibratable models
            # from the list endpoint alone, without an extra per-model
            # metadata fetch.
            "calibration_enabled": bool(self.calibration and self.calibration.get("enabled")),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.summary(),
            "inputs": [item.to_dict() for item in self.inputs],
            "outputs": [item.to_dict() for item in self.outputs],
            "string_parameters": [item.to_dict() for item in self.string_parameters],
            "calibration": self.calibration,
        }


class ModelCatalog:
    def __init__(self, models_root: Path) -> None:
        self._models_root = models_root
        self._models = self._load_models(models_root)

    @staticmethod
    def _load_models(models_root: Path) -> dict[str, ModelMetadata]:
        models: dict[str, ModelMetadata] = {}
        for metadata_path in sorted(models_root.glob("*/model.json")):
            metadata = ModelMetadata.from_file(metadata_path)
            models[metadata.id] = metadata
        return models

    @classmethod
    def from_environment(cls) -> "ModelCatalog":
        # catalog.py lives at shared/runtime/models/catalog.py -- three
        # directories below the repo root (shared/runtime/models/), unlike
        # storage.py's own parents[2] one level up in shared/runtime/.
        default_root = Path(__file__).resolve().parents[3] / "models"
        models_root = Path(os.getenv("FMU_MODELS_ROOT", str(default_root)))
        return cls(models_root=models_root)

    def list_models(self) -> list[dict[str, Any]]:
        return [model.summary() for model in self._models.values()]

    def get(self, model_id: str) -> ModelMetadata:
        try:
            return self._models[model_id]
        except KeyError as exc:
            raise KeyError(f"Unknown model: {model_id}") from exc

    def default_model_id(self) -> str:
        """Returns the canonical (GUID) id to use when no model_id is given
        on a legacy no-path-segment request. DEFAULT_MODEL_ID may be set to
        either a model's GUID `id` or its human-readable `slug` -- the
        latter is far more practical for operators/config than pasting a
        GUID into an env var, and is resolved here rather than baked into
        every config file that might set it."""
        configured = os.getenv("DEFAULT_MODEL_ID")
        if configured:
            if configured in self._models:
                return configured
            for model in self._models.values():
                if model.slug == configured:
                    return model.id
        for model in self._models.values():
            if model.slug == "SimpleVAVZone":
                return model.id
        if self._models:
            return next(iter(self._models))
        raise RuntimeError("No model metadata files were found")

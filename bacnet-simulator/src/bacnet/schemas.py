"""Pydantic request/response models for the management API.

Part of the src package — extracted from bacnet_simulator.py per GH #15
(Refactor bacnet_simulator.py), pass 1 — moved verbatim, no behavior changes.
A few of these carry a validate_*() method that raises HTTPException on bad
input (called explicitly by the route handlers, not a Pydantic validator) —
that coupling to FastAPI and to bacnet_calendar already existed in the
original file; this is a move, not a redesign.
"""
import json
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

from . import calendar as bacnet_calendar
from ..core.config import (
    EQUIPMENT_TYPES,
    LOCATION_KINDS,
    POINT_TYPES,
    SEMANTIC_PREDICATES,
    VALID_BEHAVIORS,
    VALID_OBJECT_TYPES,
    VALID_POLARITY,
    VALID_RELIABILITY,
    VALID_SEGMENTATION,
)
from ..functional_tests.validation import validate_functional_test_definition
from ..semantics.validation import validate_semantic_entity


class DeviceCreate(BaseModel):
    device_instance: int = Field(..., ge=1, le=4194302)
    name: str = Field(..., min_length=1, max_length=100)
    description: str = ""
    vendor_name: str = "Iotistica"
    model_name: str = "BACnet Simulator"
    enabled: int = 1
    firmware_revision: str = Field("N/A", max_length=50)
    protocol_revision: int = Field(22, ge=0, le=255)
    max_apdu_length_accepted: int = Field(1024, ge=50, le=1476)
    segmentation_supported: str = "segmented-both"
    location_id: Optional[int] = None
    equipment_type: Optional[str] = None
    can_receive_event_notifications: Optional[bool] = None
    simulation_mode: str = Field("simulation")
    source_device_id: Optional[int] = None
    replay_recording_id: Optional[int] = None

    def validate_device_info(self) -> None:
        if self.segmentation_supported not in VALID_SEGMENTATION:
            raise HTTPException(400, f"segmentation_supported must be one of: {sorted(VALID_SEGMENTATION)}")

    def validate_semantic(self) -> None:
        if self.equipment_type is not None and self.equipment_type not in EQUIPMENT_TYPES:
            raise HTTPException(400, f"equipment_type must be one of: {sorted(EQUIPMENT_TYPES)}")


class DeviceUpdate(DeviceCreate):
    # None means "caller didn't send this field" -- update_device() reads the
    # existing row and preserves the value rather than resetting to 'simulation'.
    simulation_mode: Optional[str] = None


class LocationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    parent_location_id: Optional[int] = None
    description: str = Field("", max_length=500)
    kind: Optional[str] = None

    def validate_semantic(self) -> None:
        if self.kind is not None and self.kind not in LOCATION_KINDS:
            raise HTTPException(400, f"kind must be one of: {sorted(LOCATION_KINDS)}")


class LocationUpdate(LocationCreate):
    pass


class EquipmentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    location_id: Optional[int] = None
    equipment_type: Optional[str] = None

    def validate_semantic(self) -> None:
        if self.equipment_type is not None and self.equipment_type not in EQUIPMENT_TYPES:
            raise HTTPException(400, f"equipment_type must be one of: {sorted(EQUIPMENT_TYPES)}")


class EquipmentUpdate(EquipmentCreate):
    pass


class ObjectCreate(BaseModel):
    object_type: str
    object_instance: int = Field(..., ge=0, le=4194302)
    name: str = Field(..., min_length=1, max_length=100)
    units: str = "no-units"
    behavior: str = "constant"
    behavior_params: str = '{"value":0}'
    enabled: int = 1
    number_of_states: int = Field(2, ge=1, le=254)
    reliability: str = "no-fault-detected"
    polarity: str = "normal"
    point_type: Optional[str] = None

    def validate_type(self):
        if self.object_type not in VALID_OBJECT_TYPES:
            raise HTTPException(400, f"Invalid object_type. Must be one of: {sorted(VALID_OBJECT_TYPES)}")
        if self.behavior not in VALID_BEHAVIORS:
            raise HTTPException(400, f"Invalid behavior. Must be one of: {sorted(VALID_BEHAVIORS)}")
        if self.reliability not in VALID_RELIABILITY:
            raise HTTPException(400, f"Invalid reliability. Must be one of: {sorted(VALID_RELIABILITY)}")
        if self.polarity not in VALID_POLARITY:
            raise HTTPException(400, f"Invalid polarity. Must be one of: {sorted(VALID_POLARITY)}")
        try:
            json.loads(self.behavior_params)
        except Exception:
            raise HTTPException(400, "behavior_params must be valid JSON")

    def validate_semantic(self) -> None:
        if self.point_type is not None and self.point_type not in POINT_TYPES:
            raise HTTPException(400, f"point_type must be one of: {sorted(POINT_TYPES)}")


class ObjectUpdate(ObjectCreate):
    pass


class SemanticEntityCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    local_slug: Optional[str] = Field(None, max_length=100)
    brick_class: str
    entity_kind: str
    device_id: Optional[int] = None
    object_id: Optional[int] = None
    location_id: Optional[int] = None
    equipment_id: Optional[int] = None

    def validate_semantic(self) -> None:
        # No semantic_key field on this model at all -- it's always
        # server-derived (see src/semantics/keys.py), never client-settable,
        # so nothing external can inject a stale or colliding key.
        try:
            validate_semantic_entity(
                self.entity_kind,
                self.brick_class,
                device_id=self.device_id,
                object_id=self.object_id,
                location_id=self.location_id,
                equipment_id=self.equipment_id,
            )
        except ValueError as e:
            raise HTTPException(400, str(e))


class SemanticEntityUpdate(SemanticEntityCreate):
    pass


class SemanticRelationshipCreate(BaseModel):
    source_entity_id: int
    predicate: str
    target_entity_id: int

    def validate_semantic(self) -> None:
        if self.predicate not in SEMANTIC_PREDICATES:
            raise HTTPException(400, f"predicate must be one of: {sorted(SEMANTIC_PREDICATES)}")
        if self.source_entity_id == self.target_entity_id:
            raise HTTPException(400, "source_entity_id and target_entity_id must differ")


class SetValueRequest(BaseModel):
    value: Any


class EnergyModelConfigCreate(BaseModel):
    model_type: str = Field(..., min_length=1)
    instance_key: str = Field("default", min_length=1, max_length=100)
    enabled: bool = True
    # Model-specific range validation (capacity >= 0, COP > 0, etc.) is not
    # duplicated here -- it's already implemented on each equipment config
    # dataclass's own .validate() method (src/energy/equipment/*.py) and is
    # applied by the router via src/energy/registry.py's MODEL_CONFIG_CLASSES.
    # This layer only checks structural shape (is model_type recognized,
    # is parameters a plain object).
    parameters: dict[str, Any] = Field(default_factory=dict)


class EnergyModelConfigUpdate(EnergyModelConfigCreate):
    pass


class NotificationClassCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    priority_to_offnormal: int = Field(100, ge=0, le=255)
    priority_to_fault: int = Field(100, ge=0, le=255)
    priority_to_normal: int = Field(100, ge=0, le=255)
    ack_required_transitions: list[str] = Field(default_factory=lambda: ["to-offnormal", "to-fault"])
    recipients: list[dict] = Field(default_factory=list)


class NotificationClassUpdate(NotificationClassCreate):
    pass


class AlarmConfigSet(BaseModel):
    notification_class_id: Optional[int] = None
    enabled: int = 1
    event_enable: list[str] = Field(default_factory=lambda: ["to-offnormal", "to-fault", "to-normal"])
    notify_type: str = "alarm"
    time_delay: int = Field(0, ge=0)
    time_delay_normal: int = Field(0, ge=0)
    params: dict = Field(default_factory=dict)


class AckAlarmRequest(BaseModel):
    ack_by: Optional[str] = None


class EventEnrollmentCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    monitored_object_id: int
    algorithm: str = "change-of-state"
    event_parameters: dict = Field(default_factory=dict)
    notification_class_id: Optional[int] = None
    enabled: int = 1
    event_enable: list[str] = Field(default_factory=lambda: ["to-offnormal", "to-fault", "to-normal"])
    notify_type: str = "event"
    time_delay: int = Field(0, ge=0)
    time_delay_normal: int = Field(0, ge=0)


class EventEnrollmentUpdate(EventEnrollmentCreate):
    pass


class TrendLogCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    monitored_object_id: int
    logging_type: str = "polled"
    # None -> resolved from the trend_log_default_* settings in create_trend_log()
    log_interval: Optional[int] = Field(None, ge=1)
    cov_increment: float = Field(1.0, ge=0)
    buffer_size: Optional[int] = Field(None, ge=1, le=100000)
    stop_when_full: int = 0
    enabled: int = 1


class TrendLogUpdate(TrendLogCreate):
    # Unlike create, update always requires explicit values — no
    # settings-default fallback for an existing, already-configured log.
    log_interval: int = Field(..., ge=1)
    buffer_size: int = Field(..., ge=1, le=100000)


class ReplayRecordingCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    # None/omitted -> "all of this device's current points" (resolved
    # server-side in Database.create_replay_recording).
    point_ids: Optional[list[int]] = None
    sample_interval_seconds: float = Field(..., gt=0)
    maximum_samples: int = Field(..., ge=1, le=100000)
    buffer_mode: str = "stop"


class ReplayRecordingUpdate(BaseModel):
    # No point_ids -- the points list is fixed at create time (every stored
    # sample references one of those rows); only operational config is
    # editable after the fact.
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    sample_interval_seconds: float = Field(..., gt=0)
    maximum_samples: int = Field(..., ge=1, le=100000)
    buffer_mode: str = "stop"


class ScheduleTargetSpec(BaseModel):
    object_id: int
    property_identifier: str = "present-value"


class ScheduleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    value_type: str = "real"
    schedule_default: Any = 0
    effective_start: Optional[str] = None
    effective_end: Optional[str] = None
    weekly_schedule: dict = Field(default_factory=dict)
    exception_schedule: list = Field(default_factory=list)
    priority_for_writing: int = Field(10, ge=1, le=16)
    enabled: int = 1
    targets: list[ScheduleTargetSpec] = Field(default_factory=list)


class ScheduleUpdate(ScheduleCreate):
    pass


class CalendarCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    date_list: list = Field(default_factory=list)
    enabled: int = 1

    def validate_date_list(self) -> None:
        try:
            bacnet_calendar.validate_date_list(self.date_list)
        except ValueError as e:
            raise HTTPException(400, str(e))


class CalendarUpdate(CalendarCreate):
    pass


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    connection_config: Optional[dict] = None
    discovery_connections: Optional[list[dict]] = None
    # Auto-generates a Building root + N/M Level locations at creation time
    # (see Database.generate_building_levels). 0/0 (the default) generates
    # nothing -- existing behavior for any caller that doesn't send these.
    above_ground_levels: int = Field(0, ge=0, le=200)
    below_ground_levels: int = Field(0, ge=0, le=100)


class ProjectUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    # None means "leave the project's saved discovery connection(s)
    # untouched" -- update_project() preserves the existing stored value
    # when this is omitted, and overwrites it when a value is given.
    connection_config: Optional[dict] = None
    discovery_connections: Optional[list[dict]] = None


class ProjectImport(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    # source_type/connection_config are NOT separate fields here: `data` is
    # exactly the JSON blob previously exported from project_json_response()
    # (see src/api/routers/exports.py), i.e. whatever save_project()/
    # update_project() embedded at the top level of profiles.data -- so an
    # export of a project that had source_type/connection_config already
    # carries them through untouched via `data` itself. import_project()
    # stores `data` verbatim; adding separate top-level params here would
    # let them silently clobber values that already exist inside `data`.
    data: dict


class DiscoveryTriggerRequest(BaseModel):
    discovery_target: Optional[str] = Field(None, max_length=255)
    device_instance_low: int = Field(0, ge=0, le=4194303)
    device_instance_high: int = Field(4194303, ge=0, le=4194303)
    timeout_ms: int = Field(5000, ge=100, le=60000)


class DiscoveryConnectionPayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    target: Optional[str] = Field(None, max_length=255)
    device_instance_low: int = Field(0, ge=0, le=4194303)
    device_instance_high: int = Field(4194303, ge=0, le=4194303)
    timeout_ms: int = Field(5000, ge=100, le=60000)
    enabled: bool = True


class Credentials(BaseModel):
    username: str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=8, max_length=200)


class PasswordReset(BaseModel):
    password: str = Field(..., min_length=8, max_length=200)


class SettingsPayload(BaseModel):
    tick_seconds: float = Field(5.0, ge=0.1, le=3600)
    device_log_maxlen: int = Field(300, ge=10, le=10000)
    global_log_maxlen: int = Field(1000, ge=10, le=50000)
    metrics_errors_maxlen: int = Field(200, ge=10, le=10000)
    metrics_new_devices_maxlen: int = Field(200, ge=10, le=10000)
    metrics_duplicate_id_maxlen: int = Field(100, ge=10, le=10000)
    metrics_traffic_feed_maxlen: int = Field(500, ge=10, le=10000)
    object_history_maxlen: int = Field(720, ge=10, le=100000)
    trend_log_default_interval: int = Field(60, ge=1)
    trend_log_default_buffer_size: int = Field(1000, ge=1, le=100000)
    jwt_expire_hours: int = Field(24, ge=1, le=8760)
    fmu_runtime_url: str = Field("http://localhost:8002", min_length=1, max_length=500)
    fmu_runtime_timeout_s: float = Field(20.0, ge=1.0, le=120.0)


class PriorityWrite(BaseModel):
    value: Any = None  # None relinquishes the slot


class FunctionalTestCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    equipment_type: str
    definition: dict

    def validate_semantic(self) -> None:
        if self.equipment_type not in EQUIPMENT_TYPES:
            raise HTTPException(400, f"equipment_type must be one of: {sorted(EQUIPMENT_TYPES)}")

    def validate_definition(self) -> None:
        try:
            validate_functional_test_definition(self.definition)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


class FunctionalTestUpdate(FunctionalTestCreate):
    pass


_CUSTOM_GRAPH_AXES = {"left", "right"}
# Only legal value this iteration -- see CustomGraphModal's plan/comments
# for why there's no real range selector yet (the /history endpoint has no
# server-side range query behind it, only a ~1h ring buffer; a preset
# picker would misrepresent what's actually available). Kept as a field,
# not omitted, so a real range control has somewhere to go later without a
# schema migration.
_CUSTOM_GRAPH_TIME_RANGES = {"live"}


def validate_custom_graph_definition(definition: dict) -> None:
    if not isinstance(definition, dict):
        raise ValueError("definition must be an object")

    series = definition.get("series")
    if not isinstance(series, list) or not series:
        raise ValueError("definition.series must be a non-empty list")

    seen: set[tuple[int, int]] = set()
    for i, entry in enumerate(series):
        if not isinstance(entry, dict):
            raise ValueError(f"definition.series[{i}] must be an object")
        device_id = entry.get("device_id")
        object_id = entry.get("object_id")
        if not isinstance(device_id, int) or isinstance(device_id, bool):
            raise ValueError(f"definition.series[{i}].device_id must be an integer")
        if not isinstance(object_id, int) or isinstance(object_id, bool):
            raise ValueError(f"definition.series[{i}].object_id must be an integer")
        key = (device_id, object_id)
        if key in seen:
            raise ValueError(f"definition.series[{i}] duplicates an already-listed point ({device_id}, {object_id})")
        seen.add(key)
        if not isinstance(entry.get("color"), str) or not entry["color"]:
            raise ValueError(f"definition.series[{i}].color must be a non-empty string")
        if entry.get("axis") not in _CUSTOM_GRAPH_AXES:
            raise ValueError(f"definition.series[{i}].axis must be one of: {sorted(_CUSTOM_GRAPH_AXES)}")
        if not isinstance(entry.get("visible"), bool):
            raise ValueError(f"definition.series[{i}].visible must be a boolean")

    if definition.get("time_range") not in _CUSTOM_GRAPH_TIME_RANGES:
        raise ValueError(f"definition.time_range must be one of: {sorted(_CUSTOM_GRAPH_TIME_RANGES)}")


class CustomGraphCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    definition: dict

    def validate_definition(self) -> None:
        try:
            validate_custom_graph_definition(self.definition)
        except ValueError as exc:
            raise HTTPException(400, str(exc))


class CustomGraphUpdate(CustomGraphCreate):
    pass

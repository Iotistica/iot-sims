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
from .config import (
    VALID_BEHAVIORS,
    VALID_OBJECT_TYPES,
    VALID_POLARITY,
    VALID_RELIABILITY,
    VALID_SEGMENTATION,
)


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

    def validate_device_info(self) -> None:
        if self.segmentation_supported not in VALID_SEGMENTATION:
            raise HTTPException(400, f"segmentation_supported must be one of: {sorted(VALID_SEGMENTATION)}")


class DeviceUpdate(DeviceCreate):
    pass


class LocationCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    parent_location_id: Optional[int] = None
    description: str = Field("", max_length=500)


class LocationUpdate(LocationCreate):
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


class ObjectUpdate(ObjectCreate):
    pass


class SetValueRequest(BaseModel):
    value: Any


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


class ProfileCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)


class ProfileUpdate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)


class ProfileImport(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: str = Field("", max_length=500)
    data: dict


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


class PriorityWrite(BaseModel):
    value: Any = None  # None relinquishes the slot

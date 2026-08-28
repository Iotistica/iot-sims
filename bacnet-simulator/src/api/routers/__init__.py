from .alarms import router as alarms_router
from .analytics import router as analytics_router
from .auth import router as auth_router
from .backups import router as backups_router
from .calendars import router as calendars_router
from .devices import router as devices_router
from .events import router as events_router
from .exports import router as exports_router
from .locations import router as locations_router
from .objects import router as objects_router
from .packet_capture import router as packet_capture_router
from .projects import router as projects_router
from .replay_recordings import router as replay_recordings_router
from .schedules import router as schedules_router
from .simulation import router as simulation_router
from .trend_logs import router as trend_logs_router
from .websocket import router as websocket_router
from .energy import router as energy_router

__all__ = [
    "alarms_router",
    "analytics_router",
    "auth_router",
    "backups_router",
    "calendars_router",
    "devices_router",
    "events_router",
    "exports_router",
    "locations_router",
    "objects_router",
    "packet_capture_router",
    "projects_router",
    "replay_recordings_router",
    "schedules_router",
    "simulation_router",
    "trend_logs_router",
    "websocket_router",
    "energy_router"
]
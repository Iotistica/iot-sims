"""Shared runtime dependency definitions for incremental migration."""
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AppContext:
    db: Any
    packet_capture: Any
    metrics: Any
    engine: Optional[Any] = None
    ws_clients: set[Any] = field(default_factory=set)
    metrics_ws_clients: set[Any] = field(default_factory=set)

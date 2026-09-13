from abc import ABC, abstractmethod
from .models import CapturedTraffic, DecodedTraffic, SimulatorAssociation

class TrafficEnricher(ABC):
    protocol: str

    @abstractmethod
    def enrich(self, traffic: CapturedTraffic, decoded: DecodedTraffic, context) -> SimulatorAssociation:
        raise NotImplementedError

class EnricherRegistry:
    def __init__(self):
        self._entries = {}

    def register(self, entry: TrafficEnricher):
        if not entry.protocol or entry.protocol in self._entries:
            raise ValueError(f"Invalid or duplicate protocol registration: {entry.protocol!r}")
        self._entries[entry.protocol] = entry

    def get(self, protocol: str) -> TrafficEnricher:
        if protocol not in self._entries:
            raise ValueError(f"Unregistered protocol: {protocol!r}")
        return self._entries[protocol]

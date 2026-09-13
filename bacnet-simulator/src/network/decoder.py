from abc import ABC, abstractmethod
from .models import CapturedTraffic, DecodedTraffic

class TrafficDecoder(ABC):
    protocol: str

    @abstractmethod
    def decode(self, traffic: CapturedTraffic) -> DecodedTraffic:
        raise NotImplementedError

class DecoderRegistry:
    def __init__(self):
        self._entries = {}

    def register(self, entry: TrafficDecoder):
        if not entry.protocol or entry.protocol in self._entries:
            raise ValueError(f"Invalid or duplicate protocol registration: {entry.protocol!r}")
        self._entries[entry.protocol] = entry

    def get(self, protocol: str) -> TrafficDecoder:
        if protocol not in self._entries:
            raise ValueError(f"Unregistered protocol: {protocol!r}")
        return self._entries[protocol]

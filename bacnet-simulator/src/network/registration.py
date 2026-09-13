"""Explicit Network protocol support contract.

A supported protocol must register both a decoder and an enricher. Recording,
protocol filtering, and serialization reject missing registrations. An enricher
may return an empty SimulatorAssociation for traffic with no simulator match;
that is distinct from an unavailable enrichment implementation. There is no
implicit enricher or fallback association provider.
"""

from .capture import TrafficCapture
from .decoder import DecoderRegistry
from .enrichment import EnricherRegistry
from .decoders.bacnet import BacnetTrafficDecoder
from .enrichers.bacnet import BacnetTrafficEnricher
from .exporters.bacnet import export_pcap

EXPORTERS = {'bacnet': export_pcap}

def create_capture(**kwargs):
    decoders, enrichers = DecoderRegistry(), EnricherRegistry()
    decoders.register(BacnetTrafficDecoder())
    enrichers.register(BacnetTrafficEnricher())
    return TrafficCapture(decoder_registry=decoders, enricher_registry=enrichers, **kwargs)

from dataclasses import fields
import struct
import pytest
from src.network.registration import create_capture
from src.network.models import CapturedTraffic, DecodedTraffic, Endpoint, SimulatorAssociation
from src.network.decoder import DecoderRegistry, TrafficDecoder
from src.network.enrichment import EnricherRegistry, TrafficEnricher
from src.network.decoders.bacnet import BacnetTrafficDecoder
from src.network.enrichers.bacnet import BacnetTrafficEnricher
from src.network.exporters.bacnet import export_pcap, internet_checksum

PAYLOAD = bytes.fromhex('81 0b 00 08 01 00 10 08')

def record(capture, **kwargs):
    args = dict(protocol='bacnet', transport='udp', direction='inbound', source=Endpoint('10.0.0.1', 47810), destination=Endpoint('10.0.0.2', 47808), payload=PAYLOAD)
    args.update(kwargs)
    capture.record(**args)

@pytest.mark.parametrize('registry,entry', [(DecoderRegistry, BacnetTrafficDecoder), (EnricherRegistry, BacnetTrafficEnricher)])
def test_strict_registries(registry, entry):
    registry = registry()
    with pytest.raises(ValueError): registry.get('missing')
    registry.register(entry())
    with pytest.raises(ValueError): registry.register(entry())
    with pytest.raises(ValueError): registry.get('BACNET')

def test_explicit_recording_and_raw_model():
    capture = create_capture()
    with pytest.raises(TypeError): capture.record(transport='udp')
    with pytest.raises(ValueError): record(capture, protocol='unknown')
    with pytest.raises(ValueError): record(capture, transport='')
    assert {f.name for f in fields(CapturedTraffic)} == {'packet_id', 'session_id', 'timestamp', 'protocol', 'transport', 'direction', 'source', 'destination', 'payload'}

def test_lifecycle_bounded_storage_and_listener():
    capture = create_capture(max_packets=2)
    seen = []
    capture.set_packet_listener(seen.append)
    record(capture)
    assert not capture.snapshot()
    first = capture.start()['session_id']
    for t in range(3): record(capture, timestamp=float(t))
    assert seen[0].timestamp == 0
    assert capture.status()['packets_seen'] == 3
    assert capture.status()['packets_dropped'] == 1
    assert len(capture.snapshot()) == 2
    capture.stop()
    record(capture)
    assert len(seen) == 3
    assert capture.start()['session_id'] != first
    assert not capture.snapshot()
    capture.clear()
    assert capture.status()['packets_seen'] == 0

def test_decoded_contract_and_operation_filter():
    capture = create_capture()
    capture.start()
    record(capture)
    decoded = BacnetTrafficDecoder().decode(capture.snapshot()[0])
    assert isinstance(decoded, DecodedTraffic)
    assert set(decoded.details) == {'bvlc', 'npdu', 'apdu', 'service'}
    result = capture.list_packets(operation='Who-Is')
    assert result['total'] == 1
    assert capture.list_packets(operation='ReadProperty')['total'] == 0
    packet = result['items'][0]
    assert packet['source'] == {'address': '10.0.0.1', 'port': 47810}
    assert packet['protocol'] == 'bacnet' and packet['transport'] == 'udp'
    assert packet['simulator'] == SimulatorAssociation().as_dict()
    assert not {'service_name', 'source_ip', 'bvlc_function', 'simulator_device_id'} & packet.keys()
    assert capture.get_packet(packet['packet_id']) == packet

def test_additive_protocol_without_core_changes():
    class Decoder(TrafficDecoder):
        protocol = 'test'
        def decode(self, traffic): return DecodedTraffic('decoded', operation='Ping')
    class Enricher(TrafficEnricher):
        protocol = 'test'
        def enrich(self, traffic, decoded, context): return SimulatorAssociation(device_id=7)
    capture = create_capture()
    capture.decoder_registry.register(Decoder())
    capture.enricher_registry.register(Enricher())
    capture.start()
    record(capture, protocol='test', transport='tcp')
    assert capture.list_packets(operation='Ping', device_id=7)['total'] == 1
    with pytest.raises(ValueError): export_pcap(capture.snapshot())

def test_bacnet_pcap_headers_payload_and_checksum():
    capture = create_capture()
    capture.start()
    record(capture, timestamp=1.5)
    data = export_pcap(capture.snapshot())
    assert struct.unpack('<IHHIIII', data[:24]) == (0xa1b2c3d4, 2, 4, 0, 0, 65535, 1)
    seconds, micros, captured, original = struct.unpack('<IIII', data[24:40])
    assert (seconds, micros) == (1, 500000)
    frame = data[40:]
    assert captured == original == len(frame)
    assert frame[42:] == PAYLOAD
    assert internet_checksum(frame[14:34]) == 0
    assert struct.unpack('!HH', frame[34:38]) == (47810, 47808)

def test_correlation_precedes_filters_and_does_not_cross_sessions():
    capture = create_capture()
    capture.start()
    # ReadProperty request, analog-input 8 / present-value, invoke 3.
    request = bytes.fromhex('81 0a 00 11 01 00 00 05 03 0c 0c 00 00 00 08 19 55')
    response = bytes.fromhex('81 0a 00 09 01 00 20 03 0c')
    class Engine:
        def resolve_wire_object(self, *, object_type, physical_instance):
            assert (object_type, physical_instance) == ('analog-input', 8)
            return {'device_id': 1, 'object_id': 8}
    context = {'engine': Engine()}
    record(capture, payload=request)
    record(capture, payload=response, direction='outbound', source=Endpoint('10.0.0.2', 47808), destination=Endpoint('10.0.0.1', 47810))
    result = capture.list_packets(direction='outbound', device_id=1, context=context)
    assert result['total'] == 1
    assert result['items'][0]['simulator']['point_id'] == 8
    capture.start(clear_existing=False)
    record(capture, payload=response, direction='outbound', source=Endpoint('10.0.0.2', 47808), destination=Endpoint('10.0.0.1', 47810))
    assert capture.list_packets(context=context)['items'][0]['simulator']['device_id'] is None

def test_recording_errors_propagate_but_listener_errors_do_not(monkeypatch):
    capture = create_capture()
    capture.start()
    def broken_listener(packet):
        raise RuntimeError('observer failed')
    capture.set_packet_listener(broken_listener)
    record(capture)
    assert len(capture.snapshot()) == 1

    def broken_packet(**kwargs):
        raise RuntimeError('packet construction failed')
    monkeypatch.setattr('src.network.capture.CapturedTraffic', broken_packet)
    with pytest.raises(RuntimeError, match='packet construction failed'):
        record(capture)


@pytest.mark.parametrize('filters', [{'protocol': 'missing'}, {'direction': 'invalid'}])
def test_invalid_filters_fail_before_serialization(monkeypatch, filters):
    capture = create_capture()
    def unexpected_serialization(**kwargs):
        pytest.fail('Invalid filters must fail before serialization')
    monkeypatch.setattr(capture, 'serialize', unexpected_serialization)
    with pytest.raises(ValueError):
        capture.list_packets(**filters)


def test_protocol_filter_excludes_other_protocol_before_decode(monkeypatch):
    class Decoder(TrafficDecoder):
        protocol = 'test'
        def decode(self, traffic):
            pytest.fail('Excluded protocol must not be decoded')
    class Enricher(TrafficEnricher):
        protocol = 'test'
        def enrich(self, traffic, decoded, context):
            pytest.fail('Excluded protocol must not be enriched')
    capture = create_capture()
    capture.decoder_registry.register(Decoder())
    capture.enricher_registry.register(Enricher())
    capture.start()
    record(capture, protocol='test')
    record(capture)
    result = capture.list_packets(protocol='bacnet')
    assert result['total'] == 1
    assert result['items'][0]['status'] is None


@pytest.mark.parametrize('payload', [PAYLOAD, bytes.fromhex('81 0a 00 11 01 00 00 05 03 0c 0c 00 00 00 08 19 55')])
def test_bacnet_operation_is_not_status(payload):
    capture = create_capture()
    capture.start()
    record(capture, payload=payload)
    decoded = BacnetTrafficDecoder().decode(capture.snapshot()[0])
    assert decoded.operation is not None
    assert decoded.status is None

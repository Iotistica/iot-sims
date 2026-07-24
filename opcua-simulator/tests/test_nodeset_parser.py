import pytest

from lib.nodeset.models import NodeSetParseError
from lib.nodeset.parser import MAX_FILE_SIZE_BYTES, parse_nodeset_xml
from tests.conftest import FIXTURES_DIR


def _read(name: str) -> bytes:
    return (FIXTURES_DIR / name).read_bytes()


def test_minimal_valid_nodeset():
    parsed = parse_nodeset_xml(_read("minimal.xml"))
    assert parsed.report.valid
    assert parsed.report.namespaces == 1
    assert parsed.report.objects == 1
    assert parsed.report.variables == 2
    assert not parsed.report.errors

    temp = parsed.nodes["ns=1;i=1001"]
    assert temp.display_name == "Temperature"
    assert temp.data_type == "i=11"  # alias "Double" resolved
    assert temp.initial_value == 72.5
    assert temp.parent_node_id == "ns=1;i=1000"

    running = parsed.nodes["ns=1;i=1002"]
    assert running.initial_value is True
    assert running.writable is True  # AccessLevel=3 -> CurrentRead|CurrentWrite


def test_nested_hierarchy_and_catch_all_variable():
    parsed = parse_nodeset_xml(_read("packaging_line.xml"))
    assert parsed.report.valid
    assert parsed.report.objects == 3          # PackagingLine, Conveyor1, Filler
    assert parsed.report.variables == 12

    conveyor_running = parsed.nodes["ns=1;i=111"]
    assert conveyor_running.parent_node_id == "ns=1;i=110"  # Conveyor1, not PackagingLine directly

    standalone = parsed.nodes["ns=1;i=200"]
    assert standalone.parent_node_id == "i=85"  # Objects folder, not in this doc -> catch-all territory


def test_unsupported_array_value_reported_not_fatal():
    parsed = parse_nodeset_xml(_read("packaging_line.xml"))
    assert parsed.report.valid
    node = parsed.nodes["ns=1;i=125"]
    assert node.initial_value is None  # array value not carried into a scalar
    assert any("ListOfDouble" in w for w in parsed.report.warnings)
    assert any("ListOfDouble" in u for u in parsed.report.unsupported_features)


def test_custom_datatype_preserved_as_raw_token_not_dropped():
    parsed = parse_nodeset_xml(_read("packaging_line.xml"))
    node = parsed.nodes["ns=1;i=126"]
    assert node.data_type == "ns=1;i=9001"  # not a built-in type, not resolved via alias, preserved as-is


def test_duplicate_node_id_detected_not_fatal():
    parsed = parse_nodeset_xml(_read("duplicate_nodeid.xml"))
    assert parsed.report.valid
    assert "ns=1;i=2" in parsed.report.duplicate_node_ids
    assert parsed.report.variables == 1  # first occurrence kept, duplicate not double-counted


def test_string_node_id_and_string_datatype():
    xml = b"""<?xml version="1.0"?>
    <UANodeSet xmlns="http://opcfoundation.org/UA/2011/03/UANodeSet.xsd"
               xmlns:uax="http://opcfoundation.org/UA/2008/02/Types.xsd">
      <NamespaceUris><Uri>http://example.org/Str</Uri></NamespaceUris>
      <UAObject NodeId="ns=1;s=Machine1" BrowseName="1:Machine1">
        <DisplayName>Machine1</DisplayName>
        <References><Reference ReferenceType="Organizes" IsForward="false">i=85</Reference></References>
      </UAObject>
      <UAVariable NodeId="ns=1;s=Machine1.Mode" BrowseName="1:Mode" DataType="String">
        <DisplayName>Mode</DisplayName>
        <References><Reference ReferenceType="HasComponent" IsForward="false">ns=1;s=Machine1</Reference></References>
        <Value><uax:String>Auto</uax:String></Value>
      </UAVariable>
    </UANodeSet>"""
    parsed = parse_nodeset_xml(xml)
    assert parsed.report.valid
    node = parsed.nodes["ns=1;s=Machine1.Mode"]
    assert node.parent_node_id == "ns=1;s=Machine1"
    assert node.initial_value == "Auto"


def test_malformed_xml_rejected():
    with pytest.raises(NodeSetParseError):
        parse_nodeset_xml(_read("malformed.xml"))


def test_xxe_attempt_rejected():
    with pytest.raises(NodeSetParseError):
        parse_nodeset_xml(_read("malicious_xxe.xml"))


def test_wrong_root_element_rejected():
    with pytest.raises(NodeSetParseError):
        parse_nodeset_xml(b"<?xml version='1.0'?><NotANodeSet/>")


def test_oversized_file_rejected():
    with pytest.raises(NodeSetParseError):
        parse_nodeset_xml(b"<UANodeSet>" + b" " * (MAX_FILE_SIZE_BYTES + 1) + b"</UANodeSet>")


def test_empty_file_rejected():
    with pytest.raises(NodeSetParseError):
        parse_nodeset_xml(b"")


def test_unresolved_reference_within_own_namespace_reported():
    xml = b"""<?xml version="1.0"?>
    <UANodeSet xmlns="http://opcfoundation.org/UA/2011/03/UANodeSet.xsd">
      <NamespaceUris><Uri>http://example.org/Unresolved</Uri></NamespaceUris>
      <UAVariable NodeId="ns=1;i=5" BrowseName="1:Orphan" DataType="Double">
        <DisplayName>Orphan</DisplayName>
        <References>
          <Reference ReferenceType="HasComponent" IsForward="false">ns=1;i=9999</Reference>
        </References>
      </UAVariable>
    </UANodeSet>"""
    parsed = parse_nodeset_xml(xml)
    assert parsed.report.unresolved_references
    assert "ns=1;i=9999" in parsed.report.unresolved_references[0]

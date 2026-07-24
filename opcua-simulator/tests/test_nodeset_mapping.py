from lib.nodeset.mapping import coerce_data_type, coerce_initial_value, default_behavior_for
from lib.nodeset.models import ParseReport


def test_coerce_known_numeric_types():
    report = ParseReport()
    assert coerce_data_type("i=1", "n1", report) == "Boolean"
    assert coerce_data_type("i=6", "n2", report) == "Int32"
    assert coerce_data_type("i=11", "n3", report) == "Double"
    assert coerce_data_type("i=12", "n4", report) == "String"
    assert not report.warnings


def test_coerce_lossy_type_warns():
    report = ParseReport()
    result = coerce_data_type("i=9", "n1", report)  # UInt64
    assert result == "Int32"
    assert any("narrowed" in w for w in report.warnings)


def test_coerce_unknown_type_falls_back_to_string_and_reports():
    report = ParseReport()
    result = coerce_data_type("ns=1;i=9001", "n1", report)
    assert result == "String"
    assert report.warnings
    assert report.unsupported_features


def test_coerce_missing_type_defaults_double():
    report = ParseReport()
    assert coerce_data_type(None, "n1", report) == "Double"
    assert report.warnings


def test_coerce_initial_value_per_type():
    assert coerce_initial_value(None, "Boolean") is False
    assert coerce_initial_value("true", "Boolean") is True
    assert coerce_initial_value(None, "Double") == 0.0
    assert coerce_initial_value("3.5", "Double") == 3.5
    assert coerce_initial_value(None, "Int32") == 0
    assert coerce_initial_value(7.9, "Int32") == 7
    assert coerce_initial_value(None, "String") == ""
    assert coerce_initial_value(42, "String") == "42"


def test_default_behavior_is_manual_seeded_with_value():
    behavior, params_json = default_behavior_for("Double", 21.5)
    assert behavior == "manual"
    assert "21.5" in params_json

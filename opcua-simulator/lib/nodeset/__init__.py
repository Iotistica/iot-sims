"""
OPC UA NodeSet2 XML import support.

See docs/nodeset-import.md for scope, supported/unsupported constructs, and
the adapter-onto-devices/tags design decision.

    parser.py    - safe XML parsing -> models.ParsedNodeSet
    models.py    - internal node/report dataclasses
    mapping.py   - NodeSet2 DataType -> simulator data_type, default behavior
    importer.py  - ParsedNodeSet -> devices/tags rows + live OPC UA nodes
"""

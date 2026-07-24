import asyncio
import json

from lib.db import Database
from lib.nodeset.importer import commit_import, plan_import
from lib.nodeset.parser import parse_nodeset_xml
from tests.conftest import FIXTURES_DIR


def _parsed(name: str):
    return parse_nodeset_xml((FIXTURES_DIR / name).read_bytes())


def test_plan_flattens_nested_objects_into_dotted_tag_names():
    plan = plan_import(_parsed("packaging_line.xml"), batch_name="Packaging Line PLC")

    by_name = {d.name: d for d in plan.devices}
    assert "PackagingLine" in by_name
    line = by_name["PackagingLine"]
    tag_names = {t.name for t in line.tags}
    assert "LineStatus" in tag_names               # direct child, no prefix
    assert "Conveyor1.Running" in tag_names         # nested Object flattened into the tag name
    assert "Filler.TankLevel" in tag_names
    assert "Filler.BatchCount" in tag_names


def test_plan_puts_bare_top_level_variables_in_catch_all_device():
    plan = plan_import(_parsed("packaging_line.xml"), batch_name="Packaging Line PLC")
    by_name = {d.name: d for d in plan.devices}
    assert "Packaging Line PLC" in by_name  # catch-all device, named after the batch
    catch_all_tags = {t.name for t in by_name["Packaging Line PLC"].tags}
    assert "StandaloneAlarm" in catch_all_tags


def test_plan_unsupported_datatype_coerces_to_string_not_dropped():
    plan = plan_import(_parsed("packaging_line.xml"), batch_name="X")
    filler = next(d for d in plan.devices if d.name == "PackagingLine")
    recipe_tag = next(t for t in filler.tags if t.name == "Filler.RecipeId")
    assert recipe_tag.data_type == "String"  # custom DataType (ns=1;i=9001) not in SIM_DATA_TYPES


class _FakeEngine:
    """Minimal stand-in for SimEngine — commit_import only needs the lock and
    the two live-mutation entry points; exercising the real asyncua server
    per test is unnecessary for the transactional/adapter logic under test."""

    def __init__(self):
        self.structural_lock = asyncio.Lock()
        self.added_devices = []
        self.added_tags = []

    async def _add_device_live_locked(self, device_row):
        self.added_devices.append(device_row["id"])

    async def _create_live_tag(self, device_id, tag_row):
        self.added_tags.append((device_id, tag_row["id"]))


def test_commit_import_is_transactional_and_creates_live_nodes(tmp_path):
    db = Database(tmp_path / "test.db")
    db.setup()
    engine = _FakeEngine()
    plan = plan_import(_parsed("packaging_line.xml"), batch_name="Packaging Line PLC")

    result = asyncio.run(commit_import(
        db, engine, plan, source_filename="packaging_line.xml", conflict_strategy="skip",
    ))

    assert not result.errors
    assert len(result.devices_created) == 2  # PackagingLine + catch-all
    assert result.tags_created == 11 + 1     # 11 nested + 1 standalone
    assert result.import_id is not None

    # DB actually has the rows (transaction committed).
    devices = db.get_devices()
    assert {d["name"] for d in devices} == {"PackagingLine", "Packaging Line PLC"}

    # Live nodes were created for everything that landed in the DB.
    assert len(engine.added_devices) == 2
    assert len(engine.added_tags) == 12

    # Import batch is tracked and deletable.
    imports = db.get_nodeset_imports()
    assert len(imports) == 1
    assert imports[0]["device_count"] == 2

    deleted_ids = db.delete_nodeset_import(result.import_id)
    assert len(deleted_ids) == 2
    assert db.get_devices() == []
    assert db.get_nodeset_imports() == []


def test_commit_import_skip_conflict_strategy_leaves_existing_device_alone(tmp_path):
    db = Database(tmp_path / "test.db")
    db.setup()
    db.create_device("PackagingLine", "pre-existing", "", "", True)
    engine = _FakeEngine()
    plan = plan_import(_parsed("packaging_line.xml"), batch_name="Packaging Line PLC")

    result = asyncio.run(commit_import(
        db, engine, plan, source_filename="packaging_line.xml", conflict_strategy="skip",
    ))

    assert "PackagingLine" in result.devices_skipped
    assert len(result.devices_created) == 1  # only the catch-all device
    assert db.get_device(1)["description"] == "pre-existing"  # untouched


def test_commit_import_reject_conflict_strategy_fails_whole_import(tmp_path):
    db = Database(tmp_path / "test.db")
    db.setup()
    db.create_device("PackagingLine", "pre-existing", "", "", True)
    engine = _FakeEngine()
    plan = plan_import(_parsed("packaging_line.xml"), batch_name="Packaging Line PLC")

    result = asyncio.run(commit_import(
        db, engine, plan, source_filename="packaging_line.xml", conflict_strategy="reject",
    ))

    assert result.errors
    assert result.devices_created == []
    assert len(db.get_devices()) == 1  # nothing new was added
    assert not engine.added_devices

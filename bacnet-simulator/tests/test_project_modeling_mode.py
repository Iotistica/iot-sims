"""Project-level modeling_mode ("standard" | "semantic") persistence.

modeling_mode is embedded in the same profiles.data JSON blob as
source_type/connection_config (no new DB column) -- these tests cover the
same round-trip/preserve-on-omit contract those fields already have, plus
the one genuinely new piece of logic: the legacy-project default."""
from __future__ import annotations


def test_save_and_load_round_trips_modeling_mode(database):
    project = database.save_project("Semantic Test", "", modeling_mode="semantic")
    assert project["modeling_mode"] == "semantic"

    loaded = database.load_project(project["id"])
    assert loaded["modeling_mode"] == "semantic"


def test_save_defaults_to_standard(database):
    project = database.save_project("Default Test", "")
    assert project["modeling_mode"] == "standard"

    loaded = database.load_project(project["id"])
    assert loaded["modeling_mode"] == "standard"


def test_update_preserves_modeling_mode_when_omitted(database):
    project = database.save_project("Preserve Test", "", modeling_mode="semantic")

    # The ordinary quick-"Save" flow (App.vue's openSave()) never passes
    # modeling_mode -- it must not silently downgrade it.
    database.update_project(project["id"], "Preserve Test Renamed", "new description")

    loaded = database.load_project(project["id"])
    assert loaded["modeling_mode"] == "semantic"


def test_update_can_explicitly_change_modeling_mode(database):
    project = database.save_project("Switch Test", "", modeling_mode="standard")

    database.update_project(project["id"], "Switch Test", "", modeling_mode="semantic")
    assert database.load_project(project["id"])["modeling_mode"] == "semantic"

    database.update_project(project["id"], "Switch Test", "", modeling_mode="standard")
    assert database.load_project(project["id"])["modeling_mode"] == "standard"


def test_legacy_project_without_modeling_mode_defaults_to_semantic(database):
    """A project saved before modeling_mode existed has no such key in its
    stored blob at all -- must default to "semantic" unconditionally, NOT
    inferred from whether it currently has any equipment/semantic_entities
    rows (older app versions may already have auto-created semantic
    entities behind the scenes, so an empty-looking project is not
    reliable evidence the user never touched semantic features)."""
    import json

    project = database.save_project("Legacy Test", "")
    with database._conn() as conn:
        row = conn.execute("SELECT data FROM profiles WHERE id=?", (project["id"],)).fetchone()
        payload = json.loads(row["data"])
        assert "modeling_mode" in payload  # sanity: save_project always writes it
        del payload["modeling_mode"]
        conn.execute("UPDATE profiles SET data=? WHERE id=?", (json.dumps(payload), project["id"]))
        conn.commit()

    loaded = database.load_project(project["id"])
    assert loaded["modeling_mode"] == "semantic"

    # Confirmed unconditional: still "semantic" even though this project has
    # zero equipment/semantic_entities rows (a genuinely empty legacy project).
    assert database.get_equipment_list() == []
    assert database.get_semantic_entities() == []

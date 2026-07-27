"""NodeSet2 XML import routes — split out of opcua_simulator.py.
See docs/nodeset-import.md for scope: Objects/Variables only, flattened
onto the existing devices/tags schema, no export yet (see that doc for why).
"""
import asyncio
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

import lib.state as state
from lib.nodeset.importer import ImportPlan, commit_import, plan_import
from lib.nodeset.models import NodeSetParseError
from lib.nodeset.parser import MAX_FILE_SIZE_BYTES, parse_nodeset_xml

router = APIRouter()


def _plan_summary(plan: ImportPlan) -> dict:
    """Shared shape between preview (nothing committed) and import (committed) responses."""
    return {
        "devices": [
            {
                "name": d.name,
                "description": d.description,
                "tag_count": len(d.tags),
                "tags": [
                    {"name": t.name, "data_type": t.data_type, "writable": t.writable}
                    for t in d.tags[:25]  # sample, not the full tree, for large imports
                ],
            }
            for d in plan.devices
        ],
        "device_count": len(plan.devices),
        "tag_count": sum(len(d.tags) for d in plan.devices),
    }


async def _read_and_parse_upload(file: UploadFile):
    xml_bytes = await file.read()
    if len(xml_bytes) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(413, f"File exceeds the {MAX_FILE_SIZE_BYTES}-byte limit")
    try:
        parsed = await asyncio.to_thread(parse_nodeset_xml, xml_bytes, file.filename or "upload.xml")
    except NodeSetParseError as e:
        raise HTTPException(400, str(e))
    return parsed


@router.post("/nodesets/preview")
async def nodeset_preview(file: UploadFile = File(...)):
    parsed = await _read_and_parse_upload(file)
    plan = plan_import(parsed, batch_name=Path(file.filename or "Imported").stem)
    return {
        "report": parsed.report.to_dict(),
        "plan": _plan_summary(plan),
    }


@router.post("/nodesets/import", status_code=201)
async def nodeset_import(
    file: UploadFile = File(...),
    name: str = Form(""),
    conflict_strategy: str = Form("skip"),
):
    if conflict_strategy not in ("skip", "reject"):
        raise HTTPException(400, "conflict_strategy must be 'skip' or 'reject' in this release")
    parsed = await _read_and_parse_upload(file)
    if not parsed.report.valid:
        raise HTTPException(400, f"NodeSet document has errors: {parsed.report.errors}")

    batch_name = name or Path(file.filename or "Imported").stem
    plan = plan_import(parsed, batch_name=batch_name)
    result = await commit_import(
        state.db, state.engine, plan, source_filename=file.filename or "upload.xml",
        conflict_strategy=conflict_strategy,
    )
    if result.errors:
        raise HTTPException(409, {"errors": result.errors, "warnings": result.warnings})

    for d in result.devices_created:
        state._device_names[d["id"]] = d["name"]
    state.log_event(0, "info", f"NodeSet import '{batch_name}': {len(result.devices_created)} device(s), "
                                f"{result.tags_created} tag(s), {len(result.devices_skipped)} skipped")

    return {
        "import_id": result.import_id,
        "devices_created": [{"id": d["id"], "name": d["name"], "tag_count": len(d["tags"])}
                             for d in result.devices_created],
        "tags_created": result.tags_created,
        "devices_skipped": result.devices_skipped,
        "parse_report": parsed.report.to_dict(),
        "warnings": result.warnings,
    }


@router.get("/nodesets/imports")
async def list_nodeset_imports():
    return await asyncio.to_thread(state.db.get_nodeset_imports)


@router.get("/nodesets/imports/{import_id}")
async def get_nodeset_import(import_id: int):
    row = await asyncio.to_thread(state.db.get_nodeset_import, import_id)
    if not row:
        raise HTTPException(404, "Import not found")
    return row


@router.delete("/nodesets/imports/{import_id}", status_code=200)
async def delete_nodeset_import(import_id: int):
    row = await asyncio.to_thread(state.db.get_nodeset_import, import_id)
    if not row:
        raise HTTPException(404, "Import not found")
    deleted_ids = await asyncio.to_thread(state.db.delete_nodeset_import, import_id)
    for device_id in deleted_ids:
        state.track(
            state.engine.delete_device_live(device_id),
            f"delete_device_live({device_id}) [nodeset import cleanup]",
        )
    blocked = [d for d in row["device_ids"] if d not in deleted_ids]
    return {"deleted_device_ids": deleted_ids, "already_removed": blocked}

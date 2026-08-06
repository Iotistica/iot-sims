"""SQLite database backup/restore for the simulator's own state.

Mirrors the pattern the Iotistica Agent (a sibling product) already uses for
this: a plain `.db` file produced via SQLite's own `VACUUM INTO` (an online,
non-blocking copy — safe under WAL mode without pausing writers), paired with
a `.meta.json` sidecar carrying a SHA-256 checksum and an integrity-check
result. Restore always verifies the candidate file first, takes an automatic
"pre-restore" safety snapshot of the current live DB, then swaps the file in
via a same-directory temp-file-then-atomic-rename.

DATA_DIR is an Azure Files (SMB) share once deployed to the cloud, not local
disk (see deploy/generate_aci.py) — every temp file used here is written
inside the same directory as its final destination before being renamed into
place, since a cross-filesystem move isn't guaranteed atomic the way a
same-directory rename is.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from ..core.config import DATA_DIR, DB_PATH

_META_SUFFIX = ".meta.json"


def get_backup_dir() -> Path:
    d = DATA_DIR / "backups" / "db"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_backup_path(file_name: str) -> Path:
    """Resolve a user-supplied filename to a path strictly inside the backup
    dir — .name strips any directory components, blocking path traversal
    (e.g. "../../etc/passwd") regardless of what the caller sends."""
    return get_backup_dir() / Path(file_name).name


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _integrity_check(path: Path) -> str:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("PRAGMA integrity_check").fetchone()
        return "ok" if row and row[0] == "ok" else f"failed: {row[0] if row else 'unknown'}"
    finally:
        conn.close()


def _write_meta(db_path: Path, label: Optional[str]) -> dict:
    meta = {
        "file_name": db_path.name,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "size_bytes": db_path.stat().st_size,
        "checksum_sha256": _sha256_file(db_path),
        "integrity": _integrity_check(db_path),
        "label": label,
    }
    meta_path = Path(str(db_path) + _META_SUFFIX)
    meta_path.write_text(json.dumps(meta, indent=2))
    return meta


def create_backup(label: Optional[str] = None) -> dict:
    """Snapshot the live DB via VACUUM INTO — an online SQLite copy, safe to
    run while the app is serving requests (no writer pause needed)."""
    backup_dir = get_backup_dir()
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    suffix = f"-{label}" if label else ""
    final_name = f"backup-{ts}{suffix}.db"
    final_path = backup_dir / final_name
    tmp_path = backup_dir / f"{final_name}.tmp"

    if tmp_path.exists():
        tmp_path.unlink()

    src_conn = sqlite3.connect(str(DB_PATH))
    try:
        src_conn.execute("VACUUM INTO ?", (str(tmp_path),))
    finally:
        src_conn.close()

    integrity = _integrity_check(tmp_path)
    if integrity != "ok":
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Backup failed integrity check: {integrity}")

    os.replace(tmp_path, final_path)
    return _write_meta(final_path, label)


def list_backups() -> list[dict]:
    backup_dir = get_backup_dir()
    entries = []
    for meta_path in backup_dir.glob(f"*{_META_SUFFIX}"):
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        db_path = backup_dir / meta.get("file_name", "")
        if db_path.exists():
            entries.append(meta)
    entries.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return entries


def verify_backup(path: Path, require_metadata: bool = True) -> dict:
    meta_path = Path(str(path) + _META_SUFFIX)
    integrity = _integrity_check(path)
    if integrity != "ok":
        raise RuntimeError(f"Backup file failed integrity check: {integrity}")
    checksum = _sha256_file(path)
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())
        expected = meta.get("checksum_sha256")
        if expected and expected != checksum:
            raise RuntimeError("Backup file checksum does not match its recorded metadata — file may be corrupt or tampered with")
    elif require_metadata:
        raise RuntimeError("No metadata found for this backup file — refusing to restore an unverified file")
    return {"integrity": integrity, "checksum_sha256": checksum}


def restore_backup(file_name: str) -> dict:
    backup_path = _safe_backup_path(file_name)
    if not backup_path.exists():
        raise FileNotFoundError(f"Backup not found: {file_name}")

    verify_backup(backup_path, require_metadata=True)

    # Safety net: snapshot the CURRENT live DB before overwriting it, so a bad
    # restore can itself be rolled back.
    pre_restore = create_backup(label="pre-restore")

    tmp_path = DATA_DIR / f"{DB_PATH.name}.restore.tmp"
    if tmp_path.exists():
        tmp_path.unlink()
    with open(backup_path, "rb") as src, open(tmp_path, "wb") as dst:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    os.replace(tmp_path, DB_PATH)

    # Stale WAL/SHM sidecars from the previous live DB no longer apply to the
    # freshly-restored file — drop them so nothing leaks in.
    for suffix in ("-wal", "-shm"):
        stale = Path(str(DB_PATH) + suffix)
        if stale.exists():
            stale.unlink()

    integrity = _integrity_check(DB_PATH)
    if integrity != "ok":
        raise RuntimeError(
            f"Restored database failed integrity check ({integrity}) — "
            f"the pre-restore snapshot '{pre_restore['file_name']}' is available to recover from."
        )

    return {"ok": True, "pre_restore_backup": pre_restore["file_name"]}


def delete_backup(file_name: str) -> bool:
    backup_path = _safe_backup_path(file_name)
    meta_path = Path(str(backup_path) + _META_SUFFIX)
    existed = backup_path.exists()
    backup_path.unlink(missing_ok=True)
    meta_path.unlink(missing_ok=True)
    return existed


def save_uploaded_backup(file_name: str, data: bytes) -> dict:
    """Accept an uploaded backup file, verify it, and commit it into the
    backup dir under a generated name (never trusting the uploaded filename
    for anything beyond a display-time extension hint)."""
    backup_dir = get_backup_dir()
    ts = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    ext = Path(file_name).suffix or ".db"
    final_name = f"uploaded-{ts}{ext}"
    final_path = backup_dir / final_name
    tmp_path = backup_dir / f"{final_name}.tmp"

    tmp_path.write_bytes(data)
    integrity = _integrity_check(tmp_path)
    if integrity != "ok":
        tmp_path.unlink(missing_ok=True)
        raise RuntimeError(f"Uploaded file is not a valid SQLite database: {integrity}")

    os.replace(tmp_path, final_path)
    return _write_meta(final_path, label="uploaded")

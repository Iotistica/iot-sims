from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from fastapi import UploadFile

from ..errors import ConflictError
from .models import Resource

_UPLOAD_CHUNK_BYTES = 8 * 1024 * 1024


class ResourceManager:
    """Generic, model-agnostic file upload -- not weather-specific, not
    scoped to any one catalog model. Exists so a model's declared
    string_parameters can include one whose value is a *file* (e.g.
    Weather's wea_filename) without every such model needing its own
    bespoke upload endpoint: the Simulation Model drawer's Parameters UI
    already renders an upload control for any parameter the catalog marks
    "is_file" (see catalog.py's StringParameterMetadata), and every one of
    them uploads through this same endpoint.

    Deliberately flat and filename-keyed (unlike DatasetManager, which is
    per-model and opaque-id-keyed) -- a resource is identified by its own
    uploaded filename, and its resolved `path` is what a caller passes
    straight through as a FMI String parameter's value."""

    def __init__(self, storage_root: Path) -> None:
        self._storage_root = storage_root

    def _final_path(self, filename: str) -> Path:
        return self._storage_root / filename

    def _metadata_path(self, filename: str) -> Path:
        return self._storage_root / f"{filename}.metadata.json"

    async def upload(self, file: UploadFile) -> Resource:
        # Path(...).name strips any directory components a malicious or
        # confused client's filename might carry (e.g. "../../etc/passwd")
        # -- resources live in one flat directory, never anywhere the
        # caller's raw filename could otherwise escape to.
        filename = Path(file.filename or "").name
        if not filename:
            raise ValueError("Uploaded file has no filename")

        self._storage_root.mkdir(parents=True, exist_ok=True)
        tmp_path = self._storage_root / f".tmp-{time.time_ns()}-{filename}"

        size_bytes = 0
        digest = hashlib.sha256()
        try:
            with tmp_path.open("wb") as out:
                while chunk := await file.read(_UPLOAD_CHUNK_BYTES):
                    out.write(chunk)
                    digest.update(chunk)
                    size_bytes += len(chunk)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        sha256 = digest.hexdigest()
        final_path = self._final_path(filename)
        if final_path.exists():
            existing = self.get(filename)
            tmp_path.unlink(missing_ok=True)
            if existing is not None and existing.sha256 == sha256:
                # Identical re-upload -- idempotent no-op, not an error.
                return existing
            raise ConflictError(
                f"Resource {filename!r} already exists with different content"
            )

        try:
            tmp_path.replace(final_path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        resource = Resource(
            filename=filename,
            path=str(final_path.resolve()),
            size_bytes=size_bytes,
            sha256=sha256,
            created_at=time.time(),
        )
        self._metadata_path(filename).write_text(
            json.dumps(resource.to_dict(), indent=2), encoding="utf-8"
        )
        return resource

    def get(self, filename: str) -> Resource | None:
        metadata_path = self._metadata_path(Path(filename).name)
        if not metadata_path.exists():
            return None
        try:
            return Resource.from_dict(json.loads(metadata_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, KeyError):
            return None

    def list(self) -> list[Resource]:
        if not self._storage_root.exists():
            return []
        resources = []
        for metadata_path in sorted(self._storage_root.glob("*.metadata.json")):
            try:
                resources.append(
                    Resource.from_dict(json.loads(metadata_path.read_text(encoding="utf-8")))
                )
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return resources

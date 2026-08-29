from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel


@dataclass
class Resource:
    """A generic, model-agnostic uploaded file, addressed by its own
    filename (not an opaque id like datasets/'s Dataset) -- deliberately
    NOT scoped to any particular catalog model or FMU variable. `path` is
    the absolute filesystem path a caller can hand straight to a FMI
    String parameter's value (confirmed: pyfmi's fmu.set() on a `fixed`
    String parameter accepts a plain absolute path directly -- no
    bare-filename-relative-to-a-magic-directory convention required for a
    pure-Modelica FMU; only Spawn/EnergyPlus-coupled models have that
    extra requirement, handled separately if/when a model needs it)."""
    filename: str
    path: str
    size_bytes: int
    sha256: str
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "filename": self.filename,
            "path": self.path,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Resource":
        return cls(
            filename=data["filename"],
            path=data["path"],
            size_bytes=data["size_bytes"],
            sha256=data["sha256"],
            created_at=data["created_at"],
        )


class ResourceResponse(BaseModel):
    filename: str
    path: str
    size_bytes: int
    sha256: str
    created_at: float


def resource_to_response(resource: Resource) -> ResourceResponse:
    return ResourceResponse(**resource.to_dict())

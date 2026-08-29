from __future__ import annotations

from fastapi import HTTPException


class ConflictError(Exception):
    """Raised for a request that's well-formed and targets a real resource,
    but can't be honored given the resource's current state (e.g. deleting a
    dataset a job still references, cancelling an already-terminal job,
    fetching results before a job has completed) -- maps to 409, distinct
    from ValueError's 400 (bad input) and KeyError's 404 (unknown resource)."""


def http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc).strip("'"))
    if isinstance(exc, ConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))

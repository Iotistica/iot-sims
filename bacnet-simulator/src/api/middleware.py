"""HTTP middleware.

Physically extracted from src/legacy.py -- continuing the GH #15 refactor,
same "moved verbatim, no behavior changes" standard as the Database and
simulation-engine extractions. Registered onto the `api` FastAPI instance
in src/application.py (a plain function here, not decorated with
`@api.middleware(...)`, since `api` doesn't exist in this module).
"""
from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from ..core.security import user_from_token

# Path prefixes reachable without a valid session — the login/setup flow
# itself, and the static admin SPA shell (the SPA then blocks on its own
# login screen until it has a token to call the real API with).
_PUBLIC_PATH_PREFIXES = ("/auth/", "/assets/")
_PUBLIC_PATHS = {"/", "/favicon.svg", "/bacnet-vendors.json"}


def _is_public_path(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PATH_PREFIXES)


async def auth_gate(request: Request, call_next):
    """Require a valid bearer token for everything except the login/setup
    flow and the static admin SPA shell (see _is_public_path)."""
    if request.method == "OPTIONS" or _is_public_path(request.url.path):
        return await call_next(request)
    auth_header = request.headers.get("authorization", "")
    if not auth_header.lower().startswith("bearer ") or not user_from_token(auth_header[7:].strip()):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    return await call_next(request)


__all__ = ["auth_gate"]

"""FastAPI dependency helpers."""
from fastapi import Request


def get_context(request: Request):
    context = getattr(request.app.state, "context", None)
    if context is None:
        raise RuntimeError("Application context has not been initialized")
    return context

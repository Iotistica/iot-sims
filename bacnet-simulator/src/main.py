"""Process entry point.

Physically extracted from src/legacy.py -- continuing the GH #15 refactor,
same "moved verbatim, no behavior changes" standard as the Database and
simulation-engine extractions.
"""
import asyncio

import uvicorn

from .application import api
from .core.config import SIM_API_PORT

__all__ = ["api", "main"]


async def main():
    config = uvicorn.Config(api, host="0.0.0.0", port=SIM_API_PORT, log_level="warning")
    server = uvicorn.Server(config)
    await server.serve()


if __name__ == "__main__":
    asyncio.run(main())

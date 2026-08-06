"""Process entry point."""
import asyncio
from .application import api
from .legacy import main

__all__ = ["api", "main"]

if __name__ == "__main__":
    asyncio.run(main())

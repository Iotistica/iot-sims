from __future__ import annotations

import logging
import os
import threading
import time

from fastapi import FastAPI

from .calibration.routes import router as calibration_router
from .datasets.routes import router as dataset_router
from .models.routes import router as models_router
from .resources.routes import router as resources_router
from .state import catalog, manager


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

app = FastAPI(title="Generic FMU Model Runtime API")
app.include_router(models_router)
app.include_router(dataset_router)
app.include_router(calibration_router)
app.include_router(resources_router)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "models": catalog.list_models(),
        "sessions": manager.session_count,
    }


def cleanup_loop() -> None:
    while True:
        time.sleep(300)
        manager.cleanup_inactive_sessions()


threading.Thread(target=cleanup_loop, daemon=True).start()

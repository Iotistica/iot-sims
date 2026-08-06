"""Device construction boundary.

Device creation currently remains a SimEngine method. This helper keeps a
stable named module for the later physical move.
"""
from typing import Any

def make_device_object(engine: Any, device: dict):
    return engine._make_device_object(device)

from __future__ import annotations

import asyncio
from typing import Any

from .resolver import CommissioningPointResolver
from .tests.ahu import run_ahu_baseline_test


class CommissioningEngine:
    def __init__(
        self,
        *,
        database: Any,
        simulation_engine: Any,
    ) -> None:
        self.database = database
        self.simulation_engine = simulation_engine
        self.resolver = CommissioningPointResolver(
            database=database,
            simulation_engine=simulation_engine,
        )
        self._results: dict[tuple[int, str], dict[str, Any]] = {}
        self._lock = asyncio.Lock()

    async def run_test(
        self,
        *,
        device_id: int,
        test_id: str,
    ) -> dict[str, Any]:
        async with self._lock:
            device = await asyncio.to_thread(
                self.database.get_device,
                device_id,
            )
            if device is None:
                raise ValueError(f"Device {device_id} was not found.")

            if test_id == "ahu-baseline":
                result = await run_ahu_baseline_test(
                    device_id=device_id,
                    resolver=self.resolver,
                )
            else:
                raise ValueError(f"Unknown commissioning test: {test_id}")

            serialized = result.to_dict()
            self._results[(device_id, test_id)] = serialized
            return serialized

    def get_results(self) -> list[dict[str, Any]]:
        return [self._results[key] for key in sorted(self._results)]

    def get_result(
        self,
        *,
        device_id: int,
        test_id: str,
    ) -> dict[str, Any] | None:
        return self._results.get((device_id, test_id))

    def reset(self) -> None:
        self._results.clear()

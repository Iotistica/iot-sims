from __future__ import annotations

from abc import ABC, abstractmethod

from ..context import FaultContext
from ..models import FaultDefinition, FaultResult


class FaultRule(ABC):
    definition: FaultDefinition

    @abstractmethod
    def evaluate(self, context: FaultContext) -> FaultResult:
        raise NotImplementedError

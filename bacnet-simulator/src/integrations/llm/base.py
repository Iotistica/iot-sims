"""Provider-neutral structured-output contract.

AI-suggestion consumers only call `.parse(...)` on the client they receive.
The selected provider (Azure OpenAI, OpenAI, or an OpenAI-compatible
endpoint) is handled by factory.py and is invisible to consumers.
"""
from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class StructuredLLMClient(Protocol):
    def parse(
        self,
        *,
        response_model: type[T],
        system_prompt: str,
        user_prompt: str,
    ) -> T: ...


class LLMNotConfiguredError(RuntimeError):
    """Raised when the selected LLM provider is missing required configuration."""

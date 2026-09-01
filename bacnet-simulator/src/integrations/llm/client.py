"""Structured-output client wrapping an already-constructed openai SDK client.

Azure OpenAI, plain OpenAI, and any OpenAI-compatible endpoint all speak the
same wire protocol through the `openai` package (`AzureOpenAI`/`OpenAI` share
the same `.beta.chat.completions.parse()` call) -- this class only knows how
to drive that call. It has no opinion on which provider `client` came from;
factory.py builds the right SDK client and hands it in.
"""
from __future__ import annotations

import time
from typing import Any, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class OpenAIStructuredClient:
    def __init__(
        self,
        client: Any,
        model: str,
        max_retries: int = 3,
    ) -> None:
        self.client = client
        self.model = model
        self.max_retries = max_retries

    def parse(
        self,
        *,
        response_model: type[T],
        system_prompt: str,
        user_prompt: str,
    ) -> T:
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                completion = self.client.beta.chat.completions.parse(
                    model=self.model,
                    temperature=0,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format=response_model,
                )

                message = completion.choices[0].message
                if message.parsed is not None:
                    return message.parsed

                refusal = getattr(message, "refusal", None)
                raise RuntimeError(
                    f"Model returned no parsed result. Refusal: {refusal!r}"
                )
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(2 ** (attempt - 1))

        raise RuntimeError(
            f"LLM request failed after {self.max_retries} attempts"
        ) from last_error

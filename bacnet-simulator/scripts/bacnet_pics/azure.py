\
from __future__ import annotations

import os
import time
from typing import TypeVar, Type

from openai import AzureOpenAI
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


class AzureStructuredClient:
    def __init__(
        self,
        deployment: str | None = None,
        max_retries: int = 3,
    ) -> None:
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        api_key = os.getenv("AZURE_OPENAI_API_KEY")
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
        self.deployment = deployment or os.getenv("AZURE_OPENAI_DEPLOYMENT")
        self.max_retries = max_retries

        missing = [
            name
            for name, value in {
                "AZURE_OPENAI_ENDPOINT": endpoint,
                "AZURE_OPENAI_API_KEY": api_key,
                "AZURE_OPENAI_DEPLOYMENT": self.deployment,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        self.client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
        )

    def parse(
        self,
        *,
        response_model: Type[T],
        system_prompt: str,
        user_prompt: str,
    ) -> T:
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                completion = self.client.beta.chat.completions.parse(
                    model=self.deployment,
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
            f"Azure OpenAI request failed after {self.max_retries} attempts"
        ) from last_error

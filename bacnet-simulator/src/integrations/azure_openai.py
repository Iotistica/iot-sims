"""Backward-compatible, env-var-only Azure OpenAI client.

Kept for scripts/bacnet_pics/azure.py's offline PICS-PDF parsing tooling,
which has no DB/Settings to read and always wants plain AZURE_OPENAI_* env
vars with zero constructor arguments. The app's own routers use
src.integrations.llm.build_llm_client instead (Settings-driven, provider-
generic); this class is not used by them anymore.

Same environment variable names as before: AZURE_OPENAI_ENDPOINT,
AZURE_OPENAI_API_KEY, AZURE_OPENAI_DEPLOYMENT (all required),
AZURE_OPENAI_API_VERSION (optional, defaults "2024-10-21").
"""
from __future__ import annotations

import os

from openai import AzureOpenAI

from .llm.client import OpenAIStructuredClient


class AzureStructuredClient(OpenAIStructuredClient):
    def __init__(
        self,
        endpoint: str | None = None,
        api_key: str | None = None,
        api_version: str | None = None,
        deployment: str | None = None,
        max_retries: int = 3,
    ) -> None:
        endpoint = endpoint or os.getenv("AZURE_OPENAI_ENDPOINT")
        api_key = api_key or os.getenv("AZURE_OPENAI_API_KEY")
        api_version = api_version or os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21")
        deployment = deployment or os.getenv("AZURE_OPENAI_DEPLOYMENT")

        missing = [
            name
            for name, value in {
                "AZURE_OPENAI_ENDPOINT": endpoint,
                "AZURE_OPENAI_API_KEY": api_key,
                "AZURE_OPENAI_DEPLOYMENT": deployment,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(
                "Missing required environment variables: " + ", ".join(missing)
            )

        sdk_client = AzureOpenAI(
            azure_endpoint=endpoint,
            api_key=api_key,
            api_version=api_version,
        )
        super().__init__(client=sdk_client, model=deployment, max_retries=max_retries)

"""Builds a StructuredLLMClient from the persisted Settings dict.

The one place that knows about providers. Config resolution per field: the
saved Settings value if non-empty, else the matching environment variable --
same fallback already used by src/integrations/azure_openai.py, so an
existing Azure-only deployment keeps working unchanged with llm_provider
defaulting to "azure_openai".
"""
from __future__ import annotations

import os
from typing import Any

from openai import AzureOpenAI, OpenAI

from .base import LLMNotConfiguredError, StructuredLLMClient
from .client import OpenAIStructuredClient


def _resolve(settings: dict[str, Any], setting_key: str, env_var: str) -> str | None:
    value = settings.get(setting_key)
    if value:
        return value
    return os.environ.get(env_var) or None


def _require(provider: str, **fields: str | None) -> None:
    missing = [name for name, value in fields.items() if not value]
    if missing:
        raise LLMNotConfiguredError(
            f"llm_provider={provider!r} is missing required configuration: "
            + ", ".join(missing)
        )


def build_llm_client(settings: dict[str, Any]) -> StructuredLLMClient:
    provider = settings.get("llm_provider") or "azure_openai"

    if provider == "azure_openai":
        endpoint = _resolve(settings, "azure_openai_endpoint", "AZURE_OPENAI_ENDPOINT")
        api_key = _resolve(settings, "azure_openai_api_key", "AZURE_OPENAI_API_KEY")
        deployment = _resolve(settings, "azure_openai_deployment", "AZURE_OPENAI_DEPLOYMENT")
        api_version = _resolve(settings, "azure_openai_api_version", "AZURE_OPENAI_API_VERSION") or "2024-10-21"
        _require(provider, endpoint=endpoint, api_key=api_key, deployment=deployment)
        sdk_client = AzureOpenAI(azure_endpoint=endpoint, api_key=api_key, api_version=api_version)
        return OpenAIStructuredClient(client=sdk_client, model=deployment)

    if provider == "openai":
        api_key = _resolve(settings, "openai_api_key", "OPENAI_API_KEY")
        model = _resolve(settings, "openai_model", "OPENAI_MODEL") or "gpt-4o-mini"
        _require(provider, api_key=api_key)
        sdk_client = OpenAI(api_key=api_key)
        return OpenAIStructuredClient(client=sdk_client, model=model)

    if provider == "openai_compatible":
        base_url = _resolve(settings, "openai_compatible_base_url", "OPENAI_COMPATIBLE_BASE_URL")
        api_key = _resolve(settings, "openai_compatible_api_key", "OPENAI_COMPATIBLE_API_KEY")
        model = _resolve(settings, "openai_compatible_model", "OPENAI_COMPATIBLE_MODEL")
        _require(provider, base_url=base_url, model=model)
        if not (base_url.startswith("http://") or base_url.startswith("https://")):
            raise LLMNotConfiguredError(
                f"llm_provider={provider!r} base_url must start with http:// or https://"
            )
        # Many self-hosted OpenAI-compatible servers (e.g. Ollama) accept any
        # non-empty string as the api_key -- the openai SDK itself requires
        # one to be set, so a real key is optional here, an empty one isn't.
        sdk_client = OpenAI(api_key=api_key or "not-needed", base_url=base_url)
        return OpenAIStructuredClient(client=sdk_client, model=model)

    raise LLMNotConfiguredError(f"Unknown llm_provider: {provider!r}")

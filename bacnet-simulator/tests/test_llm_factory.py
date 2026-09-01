"""Unit tests for src/integrations/llm/factory.py's build_llm_client -- provider
dispatch, settings-vs-environment-variable precedence, and required-field
validation for whichever provider is actually selected. AzureOpenAI/OpenAI are
monkeypatched to a recording fake at construction time, so these tests never
build a real SDK client or touch the network.
"""
from __future__ import annotations

import pytest

from src.integrations.llm import factory
from src.integrations.llm.base import LLMNotConfiguredError
from src.integrations.llm.client import OpenAIStructuredClient


class _FakeSDKClient:
    """Records the kwargs it was constructed with; stands in for AzureOpenAI/OpenAI."""
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def _fake_sdk_clients(monkeypatch):
    monkeypatch.setattr(factory, "AzureOpenAI", _FakeSDKClient)
    monkeypatch.setattr(factory, "OpenAI", _FakeSDKClient)


@pytest.fixture(autouse=True)
def _clear_llm_env(monkeypatch):
    for name in [
        "AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_API_VERSION",
        "OPENAI_API_KEY", "OPENAI_MODEL",
        "OPENAI_COMPATIBLE_BASE_URL", "OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_MODEL",
    ]:
        monkeypatch.delenv(name, raising=False)


def test_default_provider_is_azure():
    """llm_provider absent entirely -- matches a settings row saved before this
    key existed. Must still resolve to Azure, not fail or pick something else."""
    settings = {
        "azure_openai_endpoint": "https://x.openai.azure.com",
        "azure_openai_api_key": "key",
        "azure_openai_deployment": "gpt-4o-mapping",
    }
    client = factory.build_llm_client(settings)
    assert isinstance(client, OpenAIStructuredClient)
    assert client.model == "gpt-4o-mapping"
    assert isinstance(client.client, _FakeSDKClient)


def test_azure_provider_builds_azure_sdk_client():
    settings = {
        "llm_provider": "azure_openai",
        "azure_openai_endpoint": "https://x.openai.azure.com",
        "azure_openai_api_key": "key",
        "azure_openai_deployment": "gpt-4o-mapping",
        "azure_openai_api_version": "2024-10-21",
    }
    client = factory.build_llm_client(settings)
    assert client.client.kwargs == {
        "azure_endpoint": "https://x.openai.azure.com",
        "api_key": "key",
        "api_version": "2024-10-21",
    }
    assert client.model == "gpt-4o-mapping"


def test_openai_provider_builds_plain_openai_client():
    settings = {
        "llm_provider": "openai",
        "openai_api_key": "sk-test",
        "openai_model": "gpt-4o-mini",
    }
    client = factory.build_llm_client(settings)
    assert client.client.kwargs == {"api_key": "sk-test"}
    assert client.model == "gpt-4o-mini"


def test_openai_compatible_provider_uses_configured_base_url():
    settings = {
        "llm_provider": "openai_compatible",
        "openai_compatible_base_url": "http://localhost:11434/v1",
        "openai_compatible_api_key": "",
        "openai_compatible_model": "llama3.1",
    }
    client = factory.build_llm_client(settings)
    assert client.client.kwargs["base_url"] == "http://localhost:11434/v1"
    assert client.client.kwargs["api_key"]  # non-empty placeholder, never blank -- the SDK requires one
    assert client.model == "llama3.1"


def test_settings_value_overrides_environment_variable(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    settings = {"llm_provider": "openai", "openai_api_key": "settings-key", "openai_model": "gpt-4o-mini"}
    client = factory.build_llm_client(settings)
    assert client.client.kwargs["api_key"] == "settings-key"


def test_environment_variable_used_when_setting_is_empty(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("OPENAI_MODEL", "env-model")
    settings = {"llm_provider": "openai", "openai_api_key": "", "openai_model": ""}
    client = factory.build_llm_client(settings)
    assert client.client.kwargs["api_key"] == "env-key"
    assert client.model == "env-model"


def test_missing_required_field_for_selected_provider_raises():
    """openai_model has a default and is never required -- omitting only
    openai_api_key (with env vars cleared by the autouse fixture) must still
    raise, not silently fall through to some other provider's config."""
    settings = {"llm_provider": "openai", "openai_model": "gpt-4o-mini"}
    with pytest.raises(LLMNotConfiguredError):
        factory.build_llm_client(settings)


def test_unconfigured_azure_fields_dont_block_selected_openai_provider():
    """Azure fields being entirely absent must not matter when llm_provider
    is 'openai' -- only the selected provider's config is validated."""
    settings = {"llm_provider": "openai", "openai_api_key": "sk-test", "openai_model": "gpt-4o-mini"}
    client = factory.build_llm_client(settings)
    assert client.client.kwargs == {"api_key": "sk-test"}


def test_unknown_provider_is_rejected():
    with pytest.raises(LLMNotConfiguredError):
        factory.build_llm_client({"llm_provider": "not_a_real_provider"})

"""Tests for unified provider configuration utilities."""
import os
import pytest
from AstralNexus.runtime.provider_utils import (
    resolve_api_key,
    resolve_base_url,
    detect_provider_from_url,
    DEFAULT_BASE_URL,
)


class TestResolveApiKey:
    def test_explicit_key_takes_priority(self, monkeypatch):
        monkeypatch.setenv("ASTRALNEXUS_API_KEY", "env-key")
        key, source = resolve_api_key("explicit-key")
        assert key == "explicit-key"
        assert source == ""

    def test_astralnexus_env_var(self, monkeypatch):
        monkeypatch.setenv("ASTRALNEXUS_API_KEY", "astralnexus-key")
        key, source = resolve_api_key()
        assert key == "astralnexus-key"
        assert source == "ASTRALNEXUS_API_KEY"

    def test_dashscope_env_var(self, monkeypatch):
        monkeypatch.delenv("ASTRALNEXUS_API_KEY", raising=False)
        monkeypatch.setenv("DASHSCOPE_API_KEY", "dashscope-key")
        key, source = resolve_api_key()
        assert key == "dashscope-key"
        assert source == "DASHSCOPE_API_KEY"

    def test_openai_env_var(self, monkeypatch):
        monkeypatch.delenv("ASTRALNEXUS_API_KEY", raising=False)
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "openai-key")
        key, source = resolve_api_key()
        assert key == "openai-key"
        assert source == "OPENAI_API_KEY"

    def test_no_key_raises_error(self, monkeypatch):
        for var in ["ASTRALNEXUS_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY"]:
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(ValueError, match="API key not provided"):
            resolve_api_key()

    def test_priority_order(self, monkeypatch):
        monkeypatch.setenv("ASTRALNEXUS_API_KEY", "first")
        monkeypatch.setenv("DASHSCOPE_API_KEY", "second")
        monkeypatch.setenv("OPENAI_API_KEY", "third")
        key, source = resolve_api_key()
        assert key == "first"
        assert source == "ASTRALNEXUS_API_KEY"


class TestResolveBaseUrl:
    def test_explicit_url_takes_priority(self):
        url = resolve_base_url("https://custom.api.com/v1")
        assert url == "https://custom.api.com/v1"

    def test_env_var_priority(self, monkeypatch):
        monkeypatch.setenv("ASTRALNEXUS_BASE_URL", "https://env.api.com/v1")
        url = resolve_base_url()
        assert url == "https://env.api.com/v1"

    def test_config_fallback(self, monkeypatch):
        monkeypatch.delenv("ASTRALNEXUS_BASE_URL", raising=False)
        config = {"base_url": "https://config.api.com/v1"}
        url = resolve_base_url(config=config)
        assert url == "https://config.api.com/v1"

    def test_default_url(self, monkeypatch):
        monkeypatch.delenv("ASTRALNEXUS_BASE_URL", raising=False)
        url = resolve_base_url()
        assert url == DEFAULT_BASE_URL

    def test_explicit_overrides_env_and_config(self, monkeypatch):
        monkeypatch.setenv("ASTRALNEXUS_BASE_URL", "https://env.api.com")
        config = {"base_url": "https://config.api.com"}
        url = resolve_base_url("https://explicit.api.com", config)
        assert url == "https://explicit.api.com"


class TestDetectProviderFromUrl:
    def test_dashscope(self):
        assert detect_provider_from_url("https://dashscope.aliyuncs.com/compatible-mode/v1") == "dashscope"

    def test_openai(self):
        assert detect_provider_from_url("https://api.openai.com/v1") == "openai"

    def test_anthropic(self):
        assert detect_provider_from_url("https://api.anthropic.com/v1") == "anthropic"

    def test_openrouter(self):
        assert detect_provider_from_url("https://openrouter.ai/api/v1") == "openrouter"

    def test_deepseek(self):
        assert detect_provider_from_url("https://api.deepseek.com/v1") == "deepseek"

    def test_unknown(self):
        assert detect_provider_from_url("https://custom.provider.com/v1") == "unknown"

    def test_case_insensitive(self):
        assert detect_provider_from_url("https://DASHSCOPE.example.com/v1") == "dashscope"

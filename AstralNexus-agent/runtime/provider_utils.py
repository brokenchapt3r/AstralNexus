"""Unified provider configuration utilities.

Centralizes API key resolution and provider detection to eliminate
duplication between LLMClient, ModelClient, and ProviderRequestGate.
"""
from __future__ import annotations

import os
from typing import Any, Dict, Optional


# Standard provider API key environment variables (in priority order)
API_KEY_ENV_VARS = [
    "ASTRALNEXUS_API_KEY",
    "DASHSCOPE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
]

DEFAULT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def resolve_api_key(
    explicit_key: Optional[str] = None,
    provider: Optional[str] = None,
) -> tuple[str, str]:
    """Resolve API key from explicit parameter or environment variables.
    
    Args:
        explicit_key: Directly provided API key (highest priority)
        provider: Provider hint for provider-specific env vars
        
    Returns:
        Tuple of (api_key, env_var_name) where env_var_name is "" if explicit_key was used
        
    Raises:
        ValueError: If no API key is found
    """
    if explicit_key:
        return explicit_key, ""
    
    for env_var in API_KEY_ENV_VARS:
        value = os.environ.get(env_var, "").strip()
        if value:
            return value, env_var
    
    raise ValueError(
        "API key not provided. Set one of: "
        + ", ".join(API_KEY_ENV_VARS)
    )


def resolve_base_url(
    explicit_url: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> str:
    """Resolve base URL from explicit parameter, environment, or config.
    
    Args:
        explicit_url: Directly provided base URL (highest priority)
        config: Configuration dict that may contain 'base_url'
        
    Returns:
        Resolved base URL
    """
    if explicit_url:
        return explicit_url
    
    env_url = os.environ.get("ASTRALNEXUS_BASE_URL", "").strip()
    if env_url:
        return env_url
    
    if config and config.get("base_url"):
        return config["base_url"]
    
    return DEFAULT_BASE_URL


def detect_provider_from_url(base_url: str) -> str:
    """Detect provider name from base URL.
    
    Args:
        base_url: API base URL
        
    Returns:
        Provider identifier (e.g., "dashscope", "openai", "anthropic")
    """
    url_lower = base_url.lower()
    
    if "dashscope" in url_lower:
        return "dashscope"
    if "openai" in url_lower:
        return "openai"
    if "anthropic" in url_lower:
        return "anthropic"
    if "openrouter" in url_lower:
        return "openrouter"
    if "deepseek" in url_lower:
        return "deepseek"
    
    return "unknown"

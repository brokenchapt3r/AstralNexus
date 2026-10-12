from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from AstralNexus.runtime.model_selector import get_model_config, load_env_file
from AstralNexus.runtime.run_context import get_env


@dataclass(frozen=True)
class ModelClient:
    model: str | None = None
    provider: str | None = None

    def resolve(self) -> dict[str, Any]:
        """Resolve AstralNexus' own model configuration.

        This is intentionally independent from the historical host application:
        production AstralNexus must be able to run after that application is
        removed from the source tree.
        """
        load_env_file()
        config = get_model_config()
        env_model = get_env("ASTRALNEXUS_MODEL", "").strip()
        effective_model = (self.model or "").strip() or env_model or config["model"]
        effective_provider = (
            (self.provider or "").strip()
            or get_env("ASTRALNEXUS_PROVIDER", "").strip()
            or config.get("provider", "dashscope")
        )

        # Use unified provider utils for API key and base URL resolution
        from AstralNexus.runtime.provider_utils import resolve_api_key, resolve_base_url
        try:
            api_key, _ = resolve_api_key()
        except ValueError:
            api_key = ""
        
        base_url = resolve_base_url(config=config)

        return {
            "effective_model": effective_model,
            "provider": effective_provider,
            "api_key": api_key,
            "base_url": base_url,
            "config_path": config.get("config_path", ""),
            "config_source": config.get("config_source", "default"),
        }

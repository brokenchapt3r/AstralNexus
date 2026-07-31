"""Model selection interface for AstralNexus.

Provides an interactive model picker for CLI startup.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional


# Available models with metadata
AVAILABLE_MODELS = {
    "qwen3.7-max": {
        "name": "Qwen 3.7 Max",
        "description": "千问高能力通用模型，适合真实长程任务评测",
        "provider": "dashscope",
        "context_length": 1000000,
        "category": "chat",
    },
    "qwen3.7-plus": {
        "name": "Qwen 3.7 Plus",
        "description": "千问通用增强模型，适合多智能体角色执行",
        "provider": "dashscope",
        "context_length": 1000000,
        "category": "chat",
    },
    "qwen3-vl-8b-instruct": {
        "name": "Qwen3 VL 8B Instruct",
        "description": "视觉语言模型，支持图像理解",
        "provider": "dashscope",
        "context_length": 32768,
        "category": "vision",
    },
    "qwen-max": {
        "name": "Qwen Max",
        "description": "通用对话模型，性能优秀",
        "provider": "dashscope",
        "context_length": 32768,
        "category": "chat",
    },
    "qwen-plus": {
        "name": "Qwen Plus",
        "description": "平衡性能与成本",
        "provider": "dashscope",
        "context_length": 32768,
        "category": "chat",
    },
    "qwen-turbo": {
        "name": "Qwen Turbo",
        "description": "快速响应，适合简单任务",
        "provider": "dashscope",
        "context_length": 32768,
        "category": "chat",
    },
    "qwen-flash": {
        "name": "Qwen Flash",
        "description": "快速通用模型，适合低延迟多角色真实 API 测试",
        "provider": "dashscope",
        "context_length": 1000000,
        "category": "chat",
    },
}

# Capability profile consumed by Governor/Router.  Keeping these fields next
# to model metadata prevents workers from selecting a model while losing its
# context, tool, latency, or cost constraints.
MODEL_PROFILES = {
    model: {
        **info,
        "tool_calling": model not in {"qwen3-vl-8b-instruct"},
        "latency_tier": "low" if model in {"qwen-flash", "qwen-turbo"} else "standard",
        "cost_tier": "high" if "max" in model else ("low" if "flash" in model or "turbo" in model else "standard"),
        "supported_toolsets": ["file", "network", "research", "execution", "skills", "browser"],
    }
    for model, info in AVAILABLE_MODELS.items()
}

DEFAULT_MODEL = "qwen3.7-max"


def get_model_profile(model: Optional[str] = None, overrides: Optional[dict] = None) -> dict:
    """Return the resolved capability profile used to instantiate a Worker."""
    selected = str(model or get_model_config().get("model") or DEFAULT_MODEL)
    profile = dict(MODEL_PROFILES.get(selected, {
        "name": selected, "provider": "custom", "context_length": 32768,
        "tool_calling": True, "latency_tier": "unknown", "cost_tier": "unknown",
        "supported_toolsets": [],
    }))
    profile["model"] = selected
    profile.update({key: value for key, value in (overrides or {}).items() if value is not None})
    return profile


def _default_config_home() -> Path:
    # Runtime evidence and user configuration have different ownership and
    # lifecycles. ``ASTRALNEXUS_HOME`` points at run artifacts; using it here
    # made a formal CLI run lose the API key already saved by ``model set``.
    config_home = os.environ.get("ASTRALNEXUS_CONFIG_HOME", "").strip()
    if config_home:
        return Path(config_home).expanduser()
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_appdata) if local_appdata else Path.home() / "AppData" / "Local"
        return base / "AstralNexus"
    return Path.home() / ".AstralNexus"


def get_config_path(config_path: Optional[str] = None) -> Path:
    if config_path:
        return Path(config_path).expanduser()
    env_config = os.environ.get("ASTRALNEXUS_CONFIG_FILE", "").strip()
    if env_config:
        return Path(env_config).expanduser()
    return _default_config_home() / "config.yaml"


def load_env_file(env_path: Optional[str] = None) -> Optional[Path]:
    """Load AstralNexus secrets from a local .env file without overriding env."""
    candidates: list[Path] = []
    if env_path:
        candidates.append(Path(env_path).expanduser())
    env_file = os.environ.get("ASTRALNEXUS_ENV_FILE", "").strip()
    if env_file:
        candidates.append(Path(env_file).expanduser())
    candidates.append(_default_config_home() / ".env")

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            for raw_line in candidate.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and value and key not in os.environ:
                    os.environ[key] = value
            return candidate
        except OSError:
            continue
    return None


# ---------------------------------------------------------------------------
# P2-2: Unified model configuration accessor
# ---------------------------------------------------------------------------
# All modules (runtime/llm_client, demo/runner, agents/base, cli) should call
# ``get_model_config()`` instead of reading env vars / config files ad-hoc.
# Priority: env vars  >  config file  >  defaults.
# ---------------------------------------------------------------------------

def get_model_config(config_path: Optional[str] = None, overrides: Optional[dict] = None) -> dict:
    """Return the unified model configuration.

    Resolution order (highest priority first):
      1. Environment variables (``ASTRALNEXUS_MODEL``, ``ASTRALNEXUS_PROVIDER``,
         ``ASTRALNEXUS_API_KEY``)
      2. YAML config file at *config_path* (or ``~/.astralnexus/config.yaml``)
      3. Built-in defaults

    Returns a dict with keys:
      - ``model``: model ID (e.g. ``"qwen-max"``)
      - ``provider``: provider name (e.g. ``"dashscope"``)
      - ``api_key_env``: name of the API-key env var that is set (or ``""``)
      - ``config_source``: where the config was resolved from

    This is the **single source of truth** for model configuration across
    the entire AstralNexus codebase.
    """
    cfg_path = get_config_path(config_path)
    # An explicitly selected configuration owns its sibling secret file. This
    # keeps CLI runs deterministic even when they are launched by another
    # process identity whose LOCALAPPDATA/HOME points somewhere else.
    explicit_env = str(cfg_path.parent / ".env") if config_path else None
    env_file_used = load_env_file(explicit_env)

    # --- Layer 1: environment variables ---
    env_model = os.environ.get("ASTRALNEXUS_MODEL", "").strip()
    env_provider = os.environ.get("ASTRALNEXUS_PROVIDER", "").strip()
    env_api_key = ""
    for key_name in ("ASTRALNEXUS_API_KEY", "DASHSCOPE_API_KEY", "OPENAI_API_KEY",
                     "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"):
        if os.environ.get(key_name):
            env_api_key = key_name
            break

    # --- Layer 2: config file ---
    file_model = ""
    file_provider = ""
    file_base_url = ""
    if cfg_path.exists():
        try:
            import yaml
            with cfg_path.open('r', encoding='utf-8') as f:
                file_cfg = yaml.safe_load(f) or {}
            model_cfg = file_cfg.get("model", {})
            if isinstance(model_cfg, str):
                file_model = model_cfg
            elif isinstance(model_cfg, dict):
                file_model = model_cfg.get("default", "") or model_cfg.get("model", "")
                file_provider = model_cfg.get("provider", "") or file_provider
                file_base_url = model_cfg.get("base_url", "") or file_base_url
            provider_cfg = file_cfg.get("provider", {})
            if isinstance(provider_cfg, str):
                file_provider = file_provider or provider_cfg
            elif isinstance(provider_cfg, dict):
                file_provider = file_provider or provider_cfg.get("default", "")
            file_base_url = file_base_url or file_cfg.get("base_url", "")
        except Exception:
            pass

    # --- Layer 3: resolve with priority ---
    model = env_model or file_model or DEFAULT_MODEL
    provider = env_provider or file_provider or ""
    # If provider not set explicitly, infer from model metadata
    if not provider and model in AVAILABLE_MODELS:
        provider = AVAILABLE_MODELS[model].get("provider", "dashscope")
    if not provider:
        provider = "dashscope"

    source = "env" if env_model else ("file" if file_model else "default")

    result = {
        "model": model,
        "provider": provider,
        "api_key_env": env_api_key,
        "config_source": source,
        "config_path": str(cfg_path),
        "env_file": str(env_file_used) if env_file_used else "",
        "base_url": os.environ.get("ASTRALNEXUS_BASE_URL", "").strip()
        or file_base_url
        or "https://dashscope.aliyuncs.com/compatible-mode/v1",
    }
    for key in ("model", "provider", "base_url"):
        value = (overrides or {}).get(key)
        if value:
            result[key] = str(value).strip()
    if (overrides or {}).get("api_key"):
        result["api_key"] = str(overrides["api_key"])
    return result


def prompt_model_selection(current_model: str = "") -> str:
    """Interactive model selection prompt.
    
    Args:
        current_model: Currently configured model (if any)
        
    Returns:
        Selected model ID
    """
    models = list(AVAILABLE_MODELS.items())
    
    print("\n" + "=" * 60)
    print("  天枢 AstralNexus - 模型选择")
    print("=" * 60)
    
    if current_model:
        print(f"\n当前模型: {current_model}")
        if current_model in AVAILABLE_MODELS:
            info = AVAILABLE_MODELS[current_model]
            print(f"  {info['name']} - {info['description']}")
    
    print("\n可用模型:\n")
    
    for i, (model_id, info) in enumerate(models, 1):
        marker = "→" if model_id == DEFAULT_MODEL else " "
        current = " (当前)" if model_id == current_model else ""
        print(f"  {marker} {i}. {info['name']}{current}")
        print(f"      {info['description']}")
        print(f"      上下文长度: {info['context_length']:,} tokens")
        print()
    
    print(f"按 Enter 使用默认模型 ({DEFAULT_MODEL})")
    print("输入数字选择其他模型，或输入 'q' 取消")
    print()
    
    try:
        choice = input("选择 [Enter/数字/q]: ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消")
        return current_model or DEFAULT_MODEL
    
    if choice.lower() == "q":
        print("已取消")
        return current_model or DEFAULT_MODEL
    
    if not choice:
        print(f"\n使用默认模型: {DEFAULT_MODEL}")
        return DEFAULT_MODEL
    
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(models):
            selected = models[idx][0]
            print(f"\n已选择: {AVAILABLE_MODELS[selected]['name']}")
            return selected
        else:
            print(f"\n无效选择，使用默认模型: {DEFAULT_MODEL}")
            return DEFAULT_MODEL
    except ValueError:
        print(f"\n无效输入，使用默认模型: {DEFAULT_MODEL}")
        return DEFAULT_MODEL


def get_selected_model(config_path: Optional[str] = None) -> str:
    """Get the currently selected model from config or environment.
    
    Args:
        config_path: Path to config file (optional)
        
    Returns:
        Model ID
    """
    load_env_file()
    # Check environment variable first
    env_model = os.environ.get("ASTRALNEXUS_MODEL", "").strip()
    if env_model:
        return env_model
    
    # Check config file
    if config_path and os.path.exists(config_path):
        try:
            import yaml
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
            model = config.get("model", {}).get("default", "")
            if model:
                return model
        except Exception:
            pass
    
    return DEFAULT_MODEL


def save_model_choice(model_id: str, config_path: str) -> None:
    """Save model choice to config file.
    
    Args:
        model_id: Model ID to save
        config_path: Path to config file
    """
    try:
        import yaml
        
        # Load existing config
        config = {}
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f) or {}
        
        # Update model section
        if "model" not in config:
            config["model"] = {}
        config["model"]["default"] = model_id
        
        # Save
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, 'w', encoding='utf-8') as f:
            yaml.dump(config, f, allow_unicode=True, default_flow_style=False)
        
        print(f"✓ 模型选择已保存到: {config_path}")
    except Exception as e:
        print(f"✗ 保存模型选择失败: {e}")


def validate_model_config(model_id: str) -> bool:
    """Validate that a model ID is configured and available.
    
    Args:
        model_id: Model ID to validate
        
    Returns:
        True if valid and available
    """
    if not model_id:
        return False
    
    load_env_file()
    # Check API key
    api_key = (
        os.environ.get("ASTRALNEXUS_API_KEY", "").strip()
        or os.environ.get("DASHSCOPE_API_KEY", "").strip()
    )
    if not api_key:
        print("✗ 未设置 API key")
        print("  请设置环境变量 ASTRALNEXUS_API_KEY，或写入 ~/.astralnexus/.env")
        return False
    
    return True


if __name__ == "__main__":
    # Test the model picker
    import argparse
    
    parser = argparse.ArgumentParser(description="天枢模型选择工具")
    parser.add_argument("--config", help="配置文件路径")
    parser.add_argument("--save", action="store_true", help="保存选择到配置")
    args = parser.parse_args()
    
    config_path = args.config or os.path.expanduser("~/.astralnexus/config.yaml")
    current = get_selected_model(config_path)
    
    selected = prompt_model_selection(current)
    
    if args.save and selected:
        save_model_choice(selected, config_path)
    
    print(f"\n最终选择: {selected}")
    sys.exit(0 if selected else 1)

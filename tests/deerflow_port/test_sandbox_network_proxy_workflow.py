"""Adapt upstream credential-boundary checks to AstralNexus's actual CI/release.

No sandbox-network-proxy image exists here; that upstream workload is not
claimed as implemented. The original assertions are retained in b119 backups.
"""
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[2]

def test_pull_request_build_has_read_only_permissions():
    workflow=yaml.safe_load((ROOT/".github/workflows/ci.yml").read_text(encoding="utf-8"))
    assert workflow["permissions"]=={"contents":"read"}
    assert "pull_request" in workflow.get("on",workflow.get(True))
    for job in workflow["jobs"].values():
        assert job.get("permissions",workflow["permissions"])=={"contents":"read"}
        assert "secrets." not in yaml.safe_dump(job)

def test_release_credentials_are_gated_to_tag_pushes():
    path=ROOT/".github/workflows/release.yml"
    workflow=yaml.safe_load(path.read_text(encoding="utf-8"))
    assert workflow.get("on",workflow.get(True))=={"push":{"tags":["v*"]}}
    release=workflow["jobs"]["release"]
    assert release["if"]=="github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')"
    assert release["permissions"]=={"contents":"write"}
    assert release["environment"]=="release"
    publish=next(s for s in release["steps"] if s.get("name")=="Publish to PyPI")
    assert publish["env"]["TWINE_PASSWORD"]=="${{ secrets.PYPI_API_TOKEN }}"
    assert all("secrets." not in yaml.safe_dump(s) for s in release["steps"] if s is not publish)

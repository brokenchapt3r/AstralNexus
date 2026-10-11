"""Fix S: declarative node outputs must resolve to the artifacts already on disk.

Live regression source (doc 18, r13b N-003): the declared output
``deliverables/app/backend/ 下的后端源码与 schema.sql`` was treated as one
filename, so the node was failed with ``missing_outputs`` even though 14 backend
source files plus a 6.8 KB ``schema.sql`` were present.  That false negative
triggered a replan and burned recovery budget.
"""
from pathlib import Path

from AstralNexus.core.orchestrator import (
    declared_output_path_tokens,
    resolve_declared_output,
)

LIVE_DECLARATION = "deliverables/app/backend/ 下的后端源码与 schema.sql"


def _backend_tree(run_dir: Path) -> Path:
    backend = run_dir / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True)
    (backend / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (backend / "schema.sql").write_text("CREATE TABLE t(id INTEGER);\n", encoding="utf-8")
    (run_dir / "deliverables" / "app" / "requirements.txt").write_text("flask\n", encoding="utf-8")
    return backend


def test_live_declaration_yields_the_embedded_directory_token():
    assert declared_output_path_tokens(LIVE_DECLARATION) == ["deliverables/app/backend/"]
    # The bare ``schema.sql`` fragment makes no path claim on its own.
    assert "schema.sql" not in declared_output_path_tokens(LIVE_DECLARATION)


def test_live_declaration_resolves_to_the_real_backend_directory(tmp_path):
    backend = _backend_tree(tmp_path)
    resolved = resolve_declared_output(tmp_path, LIVE_DECLARATION)
    assert resolved is not None, "on-disk backend source must not be reported missing"
    assert resolved.resolve() == backend.resolve()


def test_direct_file_declaration_still_resolves(tmp_path):
    _backend_tree(tmp_path)
    resolved = resolve_declared_output(tmp_path, "deliverables/app/requirements.txt")
    assert resolved is not None
    assert resolved.name == "requirements.txt"


def test_missing_path_claim_still_fails_honestly(tmp_path):
    _backend_tree(tmp_path)
    assert declared_output_path_tokens("deliverables/app/backend/gone.py") == [
        "deliverables/app/backend/gone.py"
    ]
    assert resolve_declared_output(tmp_path, "deliverables/app/backend/gone.py") is None


def test_prose_only_declaration_makes_no_path_claim():
    assert declared_output_path_tokens("可运行的后端服务与数据库组件") == []


def test_glob_declarations_keep_the_glob_matcher():
    assert declared_output_path_tokens("models/*.js") == []


def test_backslash_directory_declaration_resolves(tmp_path):
    _backend_tree(tmp_path)
    resolved = resolve_declared_output(tmp_path, "deliverables\\app\\backend\\")
    assert resolved is not None
    assert resolved.is_dir()
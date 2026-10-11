"""Fix T: deterministic acceptance leaves must follow the FILE claims a
declared output actually makes, not the whole sentence it is written in.

Live regression source (doc 18, r13b N-003).  The node contract carried

    "deliverables/app/backend/ 下的后端源码与 schema.sql"

as one ``expected_outputs`` entry.  ``Verifier._deterministic_observations``
kept any entry whose basename contained a dot, so this became the leaf
``file:deliverables/app/backend/ 下的后端源码与 schema.sql non-empty``.
``_check_file_leaf`` resolves that literally, returns FILE_NOT_FOUND, and the
observation is recorded checked=True / holds=False — a hard "does not hold"
for 14 backend source files and a 6.8 KB ``schema.sql`` that were on disk.
Fix S removed the same false negative from the Orchestrator's artifact
postcondition; this module pins the Verifier side of that defect class.

The fix must not weaken the gate: a declared file that is genuinely absent
still has to fail, and a prose or directory declaration must stay undecidable
rather than become a fabricated pass.
"""
from __future__ import annotations

from AstralNexus.core.output_paths import declared_output_file_claims
from AstralNexus.core.task_graph import TaskGraph, TaskNode
from AstralNexus.core.verifier import Verifier
from AstralNexus.runtime.trace import TraceRecorder

LIVE_DECLARATION = "deliverables/app/backend/ 下的后端源码与 schema.sql"

# The r13b PATCH-R001-BACKEND contract, verbatim from the run's task_graph.
LIVE_EXPECTED_OUTPUTS = [
    "deliverables/app/backend/ 下的后端源码与 schema.sql",
    "deliverables/app/backend/ 下的幂等种子初始化脚本",
    "deliverables/app/backend/ 下由初始化产生的 SQLite 运行库文件",
    "deliverables/app/requirements.txt",
    "deliverables/app/start.bat",
    "deliverables/app/backend/schema.sql",
    "deliverables/app/backend/db.py",
]


def _verifier(tmp_path, graph=None):
    return Verifier(
        graph or TaskGraph(), TraceRecorder(tmp_path),
        llm_client=None, workspace_root=tmp_path,
    )


def _backend_tree(run_dir):
    backend = run_dir / "deliverables" / "app" / "backend"
    backend.mkdir(parents=True)
    (backend / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (backend / "db.py").write_text("DB = 'maint_demo.db'\n", encoding="utf-8")
    (backend / "schema.sql").write_text("CREATE TABLE t(id INTEGER);\n", encoding="utf-8")
    app = run_dir / "deliverables" / "app"
    (app / "requirements.txt").write_text("flask\n", encoding="utf-8")
    (app / "start.bat").write_text("@echo off\n", encoding="utf-8")
    return backend


# --- claim extraction -------------------------------------------------------

def test_prose_declaration_claims_no_file():
    """The embedded token is a DIRECTORY, and a bare ``schema.sql`` with no
    directory part makes no path claim — so this sentence yields no leaf."""
    assert declared_output_file_claims(LIVE_DECLARATION) == []


def test_plain_path_declaration_is_still_claimed():
    assert declared_output_file_claims("deliverables/app/requirements.txt") == [
        "deliverables/app/requirements.txt"
    ]
    assert declared_output_file_claims("deliverables/app/backend/schema.sql") == [
        "deliverables/app/backend/schema.sql"
    ]


def test_embedded_real_path_is_recovered_from_prose():
    """Previously this produced ``file:<sentence>`` and a false FILE_NOT_FOUND."""
    assert declared_output_file_claims(
        "deliverables/runs/api/core-api-calls.txt 与对应截图"
    ) == ["deliverables/runs/api/core-api-calls.txt"]


def test_directory_glob_and_artifact_declarations_claim_nothing():
    assert declared_output_file_claims("deliverables/app/backend/") == []
    assert declared_output_file_claims("deliverables/app/tests") == []
    assert declared_output_file_claims("deliverables/app/models/*.js") == []
    assert declared_output_file_claims("artifact:backend-service") == []
    assert declared_output_file_claims("") == []


def test_backslash_declaration_is_normalized():
    assert declared_output_file_claims("deliverables\\app\\requirements.txt") == [
        "deliverables/app/requirements.txt"
    ]


# --- verifier dimension -----------------------------------------------------

def test_live_contract_reports_no_phantom_gap(tmp_path):
    """The r13b contract against the r13b on-disk tree: every decided leaf
    holds, and nothing is reported FILE_NOT_FOUND for a file that exists."""
    _backend_tree(tmp_path)
    node = TaskNode(
        node_id="PATCH-R001-BACKEND", goal="backend", role="coder",
        metadata={"expected_outputs": LIVE_EXPECTED_OUTPUTS},
    )
    dim = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    assert dim is not None
    reasons = [o.get("reason") for o in dim.observations]
    assert "FILE_NOT_FOUND" not in reasons, dim.details
    assert dim.score == 1.0 and dim.passed
    # Only the four explicit file paths became leaves; the three prose /
    # directory declarations stayed undecidable.
    assert sorted(o["criterion_id"] for o in dim.observations) == [
        "PATCH-R001-BACKEND.EO04",
        "PATCH-R001-BACKEND.EO05",
        "PATCH-R001-BACKEND.EO06",
        "PATCH-R001-BACKEND.EO07",
    ]


def test_genuinely_missing_declared_file_still_fails(tmp_path):
    """Fix T must not turn a real gap into a pass."""
    _backend_tree(tmp_path)
    node = TaskNode(
        node_id="D-3", goal="backend", role="coder",
        metadata={"expected_outputs": ["deliverables/app/backend/seed.py"]},
    )
    dim = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    assert dim is not None and dim.score == 0.0 and not dim.passed
    assert dim.observations[0]["reason"] == "FILE_NOT_FOUND"


def test_empty_declared_file_still_fails(tmp_path):
    backend = _backend_tree(tmp_path)
    (backend / "seed.py").write_text("", encoding="utf-8")
    node = TaskNode(
        node_id="D-4", goal="backend", role="coder",
        metadata={"expected_outputs": ["deliverables/app/backend/seed.py"]},
    )
    dim = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    assert dim is not None and dim.score == 0.0
    assert dim.observations[0]["reason"] == "FILE_EMPTY"


def test_prose_only_contract_adds_no_dimension(tmp_path):
    """No file claim at all leaves the node entirely to the semantic layer,
    rather than inventing either a pass or a gap."""
    _backend_tree(tmp_path)
    node = TaskNode(
        node_id="D-5", goal="backend", role="coder",
        metadata={"expected_outputs": [LIVE_DECLARATION, "deliverables/app/backend/"]},
    )
    assert _verifier(tmp_path)._verify_deterministic_acceptance(node) is None


def test_single_claim_keeps_the_unsuffixed_criterion_id(tmp_path):
    """Backward compatibility with the existing EO numbering."""
    _backend_tree(tmp_path)
    node = TaskNode(
        node_id="D-6", goal="backend", role="coder",
        metadata={"expected_outputs": ["deliverables/app/requirements.txt"]},
    )
    dim = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    assert [o["criterion_id"] for o in dim.observations] == ["D-6.EO01"]


def test_multiple_claims_in_one_declaration_are_numbered(tmp_path):
    _backend_tree(tmp_path)
    node = TaskNode(
        node_id="D-7", goal="backend", role="coder",
        metadata={"expected_outputs": [
            "deliverables/app/backend/db.py 与 deliverables/app/backend/schema.sql",
        ]},
    )
    dim = _verifier(tmp_path)._verify_deterministic_acceptance(node)
    assert [o["criterion_id"] for o in dim.observations] == ["D-7.EO01.1", "D-7.EO01.2"]
    assert dim.score == 1.0

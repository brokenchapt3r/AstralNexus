"""Fix U: a declared output may only contribute paths it actually contains.

``Orchestrator._execute_node`` turns ``expected_outputs`` into
``expected_artifact_paths`` — the concrete filesystem postconditions a Worker is
told to materialize.  The block's own comment says prose must never become a
path, "that creates an impossible contract which a real Worker cannot satisfy",
but the guard only rejected prose WITHOUT a separator::

    basename        = Path(normalized.rstrip("/")).name
    has_file_extension = bool(Path(basename).suffix)
    has_path_separator = "/" in normalized
    if is_directory_path or (has_path_separator and has_file_extension): ...

The live r13b N-003 declaration ``deliverables/app/backend/ 下的后端源码与
schema.sql`` has a separator AND a ``.sql`` basename suffix, so the whole
sentence was bound as a postcondition path and pushed into the Worker contract
(and, via ``expected_artifact_paths``, into the artifact postcondition check).

Fix U binds the paths the declaration contains.  Globs and directory
declarations keep their existing handling, and a clean single path is
byte-identical to before.
"""
from __future__ import annotations

from pathlib import Path

from AstralNexus.core.orchestrator import declared_output_path_tokens

LIVE_DECLARATION = "deliverables/app/backend/ 下的后端源码与 schema.sql"


def _bind(expected_outputs):
    """The Fix U binding rule, mirroring core/orchestrator.py."""
    bound = []
    for raw_output in expected_outputs:
        output_text = str(raw_output).strip()
        if not output_text:
            continue
        normalized_output = output_text.replace("\\", "/")
        basename = Path(normalized_output.rstrip("/")).name
        is_directory_path = normalized_output.endswith("/")
        has_file_extension = bool(Path(basename).suffix)
        has_path_separator = "/" in normalized_output
        is_glob = any(char in normalized_output for char in "*?[")
        if is_directory_path or is_glob:
            bound.append(normalized_output)
        elif has_path_separator and has_file_extension:
            embedded = declared_output_path_tokens(normalized_output)
            if embedded == [normalized_output]:
                bound.append(normalized_output)
            else:
                bound.extend(embedded)
    return list(dict.fromkeys(bound))


def test_prose_declaration_binds_its_directory_not_the_sentence():
    bound = _bind([LIVE_DECLARATION])
    assert bound == ["deliverables/app/backend/"]
    assert LIVE_DECLARATION not in bound


def test_clean_paths_are_unchanged():
    outputs = [
        "deliverables/app/requirements.txt",
        "deliverables/app/start.bat",
        "deliverables/app/backend/schema.sql",
    ]
    assert _bind(outputs) == outputs


def test_directory_declaration_is_unchanged():
    assert _bind(["deliverables/app/backend/"]) == ["deliverables/app/backend/"]


def test_glob_declaration_keeps_its_matcher():
    assert _bind(["deliverables/app/frontend/*.js"]) == ["deliverables/app/frontend/*.js"]


def test_prose_without_separator_still_binds_nothing():
    """The original guard's job is preserved."""
    assert _bind(["基础配置与模板文件生成成功"]) == []
    assert _bind(["O-01"]) == []


def test_embedded_file_path_is_recovered_from_prose():
    assert _bind(["后端源码与 deliverables/app/backend/schema.sql 已就绪"]) == [
        "deliverables/app/backend/schema.sql"
    ]


def test_live_r13b_contract_contains_no_impossible_path():
    live = [
        "deliverables/app/backend/ 下的后端源码与 schema.sql",
        "deliverables/app/backend/ 下的幂等种子初始化脚本",
        "deliverables/app/backend/ 下由初始化产生的 SQLite 运行库文件",
        "deliverables/app/requirements.txt",
        "deliverables/app/start.bat",
        "deliverables/app/backend/schema.sql",
    ]
    bound = _bind(live)
    assert bound == [
        "deliverables/app/backend/",
        "deliverables/app/requirements.txt",
        "deliverables/app/start.bat",
        "deliverables/app/backend/schema.sql",
    ]
    # Every bound entry is a path a Worker can actually create.
    for path in bound:
        assert " " not in Path(path.rstrip("/")).name, path

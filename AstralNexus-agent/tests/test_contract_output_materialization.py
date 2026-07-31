from pathlib import Path

from AstralNexus.cli.main import (
    _materialize_contract_outputs,
    _materialize_tool_contract_outputs,
    _missing_contract_output_names,
    _unsupported_forbidden_claims,
)


def test_forbidden_policy_allows_honest_missing_evidence_statement():
    marker = "声称已完成测试但无测试日志或调用记录"
    response = "当前尚未完成测试，缺少测试日志或调用记录，不得声称已完成测试但无测试日志或调用记录。"
    assert _unsupported_forbidden_claims(response, [marker]) == []


def test_forbidden_policy_rejects_unsupported_completion_claim():
    marker = "声称已完成测试但无测试日志或调用记录"
    response = "系统声称已完成测试但无测试日志或调用记录。"
    assert _unsupported_forbidden_claims(response, [marker]) == [marker]


def test_materialize_multiple_contract_outputs_by_top_level_heading(tmp_path: Path):
    response = """# 主文

正文。

# 数据摘要（Markdown）

摘要内容。

# 验收报告（Markdown）

验收内容。
"""
    refs, missing = _materialize_contract_outputs(
        response=response,
        required_outputs=[
            {"output_id": "O-01", "name": "数据摘要（Markdown）", "type": "text"},
            {"output_id": "O-02", "name": "验收报告（Markdown）", "type": "text"},
        ],
        delivery_dir=tmp_path,
    )

    assert missing == []
    assert refs == [
        "deliverables/数据摘要（Markdown）.md",
        "deliverables/验收报告（Markdown）.md",
    ]
    assert (tmp_path / "数据摘要（Markdown）.md").read_text(encoding="utf-8").startswith(
        "# 数据摘要（Markdown）"
    )
    assert "验收内容" in (tmp_path / "验收报告（Markdown）.md").read_text(encoding="utf-8")


def test_single_contract_output_keeps_whole_response(tmp_path: Path):
    refs, missing = _materialize_contract_outputs(
        response="没有标题但这是完整交付。\n",
        required_outputs=[{"output_id": "O-01", "name": "报告", "type": "text"}],
        delivery_dir=tmp_path,
    )

    assert missing == []
    assert len(refs) == 1
    assert (tmp_path / "报告.md").read_text(encoding="utf-8") == "没有标题但这是完整交付。\n"


def test_nested_file_contract_preserves_declared_path_and_extension(tmp_path: Path):
    refs, missing = _materialize_contract_outputs(
        response=(
            "# src/calculator.py\n\n"
            "```python\nVALUE = 1\n```\n\n"
            "# DELIVERY.md\n\n说明。\n"
        ),
        required_outputs=[
            {"name": "src/calculator.py", "type": "file"},
            {"name": "DELIVERY.md", "type": "file"},
        ],
        delivery_dir=tmp_path,
    )

    assert missing == []
    assert refs == ["deliverables/src/calculator.py", "deliverables/DELIVERY.md"]
    assert (tmp_path / "src" / "calculator.py").is_file()
    assert (tmp_path / "DELIVERY.md").is_file()


def test_tool_written_nested_outputs_bind_to_contract(tmp_path: Path):
    source = tmp_path / "src" / "calculator.py"
    tests = tmp_path / "tests" / "test_calculator.py"
    source.parent.mkdir()
    tests.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    tests.write_text("def test_value(): assert 1 == 1\n", encoding="utf-8")

    refs, missing = _materialize_tool_contract_outputs(
        required_outputs=[
            {"name": "src/calculator.py", "type": "file"},
            {"name": "tests/test_calculator.py", "type": "file"},
        ],
        delivery_dir=tmp_path,
        artifact_paths=[str(source), str(tests)],
    )

    assert missing == []
    assert refs == [
        "deliverables/src/calculator.py",
        "deliverables/tests/test_calculator.py",
    ]


def test_multiple_contract_outputs_report_missing_heading(tmp_path: Path):
    refs, missing = _materialize_contract_outputs(
        response="# 只有一个输出\n\n内容。\n",
        required_outputs=[
            {"output_id": "O-01", "name": "第一个", "type": "text"},
            {"output_id": "O-02", "name": "第二个", "type": "text"},
        ],
        delivery_dir=tmp_path,
    )

    assert refs == []
    assert missing == ["第一个", "第二个"]


def test_contract_heading_matching_allows_format_suffix_and_numbering(tmp_path: Path):
    refs, missing = _materialize_contract_outputs(
        response="# 1. 专利文本\n\n正文。\n\n# 2. 创新点说明\n\n创新点。\n",
        required_outputs=[
            {"output_id": "O-01", "name": "专利文本（Markdown）", "type": "text"},
            {"output_id": "O-02", "name": "创新点说明（Markdown）", "type": "text"},
        ],
        delivery_dir=tmp_path,
    )

    assert missing == []
    assert len(refs) == 2


def test_first_aggregate_output_can_use_prefix_before_later_output_heading(tmp_path: Path):
    refs, missing = _materialize_contract_outputs(
        response="# 摘要\n\n专利正文。\n\n# 创新点说明\n\n创新点正文。\n",
        required_outputs=[
            {"output_id": "O-01", "name": "专利文本（Markdown）", "type": "text"},
            {"output_id": "O-02", "name": "创新点说明（Markdown）", "type": "text"},
        ],
        delivery_dir=tmp_path,
    )

    assert missing == []
    assert "专利正文" in (tmp_path / "专利文本（Markdown）.md").read_text(encoding="utf-8")


def test_missing_output_heading_is_detected_before_materialization():
    missing = _missing_contract_output_names(
        "# 元信息\n\n字段。\n",
        [
            {"name": "元信息", "required": True},
            {"name": "使用说明", "required": True},
        ],
    )
    assert missing == ["使用说明"]


def test_same_level_internal_sections_stay_inside_parent_output(tmp_path: Path):
    refs, missing = _materialize_contract_outputs(
        response=(
            "## 专利文本（Markdown）\n\n"
            "## 摘要\n\n摘要内容。\n\n"
            "## 权利要求书\n\n权利要求内容。\n\n"
            "## 创新点说明（Markdown）\n\n"
            "## 区别特征列表\n\n区别内容。\n"
        ),
        required_outputs=[
            {"name": "专利文本（Markdown）", "type": "text"},
            {"name": "创新点说明（Markdown）", "type": "text"},
        ],
        delivery_dir=tmp_path,
    )
    assert missing == []
    assert "权利要求内容" in (tmp_path / "专利文本（Markdown）.md").read_text(encoding="utf-8")
    assert "区别内容" in (tmp_path / "创新点说明（Markdown）.md").read_text(encoding="utf-8")

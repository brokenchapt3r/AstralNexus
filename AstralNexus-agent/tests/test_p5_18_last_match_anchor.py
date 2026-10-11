"""Fix P5#18 (r17-resume#3 live, 2026-10-03): tests_passed 锚定取最后一个决定性匹配回执。

实地（gen-8 R-SEED-CANON-INPROC-ONESHOT，恢复合同 13:10 事实基线 (a)）：同一冻结
回执集内命令匹配 ``python deliverables/app/backend/seed_data.py --check`` 的回执不
唯一——执行早期 dev 试跑 rc=1（receipt 70f07d1a）与最终锚定 rc=0（be1d579f）并存；
旧实现首个决定性匹配即返回，AC07 被早期失败回执永久毒化判 TEST_EXECUTION_FAILED，
rc=0 的权威锚定从未被消费。gen-6 worker 曾以只读加载本模块预测该机制，gen-8 字段
级证实。修复：最后一个决定性匹配为权威锚定（合同普遍规定锚定执行是全部写操作后的
最后一条顶层命令；对称地，更晚的失败执行也作废更早的通过——staleness 原则）。
"""
from __future__ import annotations

import json

from AstralNexus.runtime.acceptance_checks import run_acceptance_checks

CMD = "python deliverables/app/backend/seed_data.py --check"


def _receipt(command, *, returncode=0, status="success", stdout_tail="",
             files=None, rid="rcpt"):
    return {
        "tool": "terminal_exec",
        "status": status,
        "returncode": returncode,
        "stdout_tail": stdout_tail,
        "files": files or {},
        "receipt_id": rid,
        "arguments": json.dumps({"command": command}),
    }


def _obs(criteria, receipts, **kwargs):
    return run_acceptance_checks(criteria, receipts=receipts, **kwargs)


def test_early_failed_trial_does_not_poison_later_passing_anchor():
    """gen-8 field case: first match rc=1 (dev trial), last match rc=0 with a
    passing summary — the LAST decisive receipt anchors and the criterion
    holds.  Under the old first-match rule this was TEST_EXECUTION_FAILED."""
    receipts = [
        _receipt(CMD, returncode=1, status="failed",
                 stdout_tail="Traceback (most recent call last): ...", rid="early"),
        _receipt(CMD, returncode=0, stdout_tail="13 passed, 0 failed", rid="final"),
    ]
    results = _obs([f"tests_passed:{CMD}"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is True
    assert results[0]["receipt_ids"] == ["final"]


def test_later_failure_supersedes_earlier_pass():
    """Symmetric staleness principle: the last decisive execution decides — an
    earlier green run cannot launder a final red one."""
    receipts = [
        _receipt(CMD, returncode=0, stdout_tail="13 passed", rid="early"),
        _receipt(CMD, returncode=1, status="failed", stdout_tail="", rid="final"),
    ]
    results = _obs([f"tests_passed:{CMD}"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is False
    assert results[0]["reason"] == "TEST_EXECUTION_FAILED"
    assert results[0]["receipt_ids"] == ["final"]


def test_last_unrecognized_summary_is_not_rescued_by_earlier_pass():
    receipts = [
        _receipt(CMD, returncode=0, stdout_tail="13 passed", rid="early"),
        _receipt(CMD, returncode=0, stdout_tail="SUMMARY rounds=2 ready=2/2",
                 rid="final"),
    ]
    results = _obs([f"tests_passed:{CMD}"], receipts)
    assert results[0]["checked"] is False
    assert results[0]["holds"] is None
    assert results[0]["reason"] == "TEST_SUMMARY_NOT_RECOGNIZED"


def test_json_summary_anchor_with_earlier_noise_passes():
    """The canonical single-line JSON summary (#17b vocabulary) anchors when it
    is the last decisive receipt, regardless of earlier dev noise."""
    line = json.dumps({
        "command": CMD, "returncode": 0,
        "checks": [{"name": "c1", "status": "ok", "observed": 1}],
        "passed": 4, "failed": 0, "generated_at": "2026-10-03T13:00:00Z",
    })
    receipts = [
        _receipt("python -c \"import seed_data\"", stdout_tail="import ok",
                 rid="noise-unmatched"),
        _receipt(CMD, returncode=1, status="failed", stdout_tail="boom", rid="trial"),
        _receipt(CMD, returncode=0, stdout_tail=f"{line}\n", rid="anchor"),
    ]
    results = _obs([f"tests_passed:{CMD}"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is True
    assert results[0]["receipt_ids"] == ["anchor"]


def test_ambiguous_only_still_unverified():
    """No decisive match: ambiguity path unchanged by #18.  Unbalanced quotes
    make shell provenance unestablishable (_split_segments -> None)."""
    receipts = [
        _receipt("sh -c 'python checks/x.py && tail", stdout_tail="1 passed",
                 rid="amb"),
    ]
    results = _obs(["tests_passed:python checks/x.py"], receipts)
    assert results[0]["checked"] is False
    assert results[0]["reason"] == "TEST_COMMAND_MATCH_UNVERIFIED"


def test_no_match_still_not_found():
    results = _obs([f"tests_passed:{CMD}"], [])
    assert results[0]["checked"] is False
    assert results[0]["reason"] == "TEST_EXECUTION_NOT_FOUND"


def test_decisive_anchor_overrides_earlier_ambiguity():
    """An earlier ambiguous receipt does not downgrade a later decisive anchor
    (old behavior: first decisive match won regardless of prior ambiguity —
    preserved)."""
    receipts = [
        _receipt("sh -c 'python checks/x.py && tail", stdout_tail="1 passed",
                 rid="amb"),
        _receipt("python checks/x.py", stdout_tail="5 passed", rid="anchor"),
    ]
    results = _obs(["tests_passed:python checks/x.py"], receipts)
    assert results[0]["checked"] is True
    assert results[0]["holds"] is True
    assert results[0]["receipt_ids"] == ["anchor"]


def test_stale_check_applies_to_last_anchor():
    """Staleness is evaluated against the LAST anchor's versions: the final
    execution's file snapshot differs from the latest baseline -> STALE."""
    receipts = [
        _receipt(CMD, stdout_tail="13 passed", rid="early",
                 files={"a.py": {"sha256": "old", "bytes": 1}}),
        _receipt(CMD, stdout_tail="13 passed", rid="final",
                 files={"a.py": {"sha256": "mid", "bytes": 1}}),
    ]
    results = _obs(
        [f"tests_passed:{CMD}"], receipts,
        latest_files={"a.py": {"sha256": "newest", "bytes": 1}},
    )
    assert results[0]["checked"] is True
    assert results[0]["holds"] is False
    assert results[0]["reason"] == "TEST_EXECUTION_STALE"
    assert results[0]["receipt_ids"] == ["final"]

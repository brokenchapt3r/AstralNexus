"""Explicit standalone content audit, never the default run gate."""
from AstralNexus.core.delivery_acceptance import _unsupported_forbidden_claims


def test_forbidden_policy_allows_honest_negative_disclosure():
    marker="声称已完成测试但无测试日志或调用记录"
    assert _unsupported_forbidden_claims("当前尚未完成测试，缺少记录，不得"+marker,[marker])==[]


def test_forbidden_policy_rejects_unsupported_completion_claim():
    marker="声称已完成测试但无测试日志或调用记录"
    assert _unsupported_forbidden_claims("系统"+marker,[marker])==[marker]

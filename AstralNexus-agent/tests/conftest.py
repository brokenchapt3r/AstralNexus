"""Pytest configuration for AstralNexus tests.

AstralNexus competition delivery uses real LLM execution only. Tests no longer
force offline fallback; live role-execution tests require model credentials via
ASTRALNEXUS_API_KEY or DASHSCOPE_API_KEY.
"""

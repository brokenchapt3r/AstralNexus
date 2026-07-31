"""Shared capability vocabulary used by Governor validation and adapters."""
from __future__ import annotations

from collections.abc import Iterable


IMPLEMENTATION_MARKERS = (
    "api", "service", "backend", "frontend", "full-stack", "full stack",
    "database", "sql", "code", "coding", "software", "application",
    "interface", "implement", "build", "develop", "create", "write",
    "deploy", "debug", "test", "接口", "服务", "后端", "前端", "数据库",
    "代码", "软件", "应用", "实现", "构建", "开发", "编写", "部署", "修复",
)

RETRIEVAL_MARKERS = (
    "retrieve", "retrieval", "search", "document", "knowledge base",
    "knowledge_base", "检索", "搜索", "文档", "知识库", "资料采集",
)

IMPLEMENTATION_CAPABILITIES = frozenset({
    "code_generation", "programming", "code_modification", "debugging",
    "refactoring", "test_writing", "api_design", "api_integration",
    "backend_development", "frontend_development", "database_design",
    "web_app_development", "software_implementation", "implementation",
    "full_stack_development", "ui_development", "sql_design",
    "database_migration", "api_testing", "integration_testing",
    "deployment", "containerization", "build_automation",
    "dependency_management", "test_orchestration",
})

CAPABILITY_CATEGORIES = {
    "retrieval": ("retrieval", "search", "lookup", "fetch", "query"),
    "research": (
        "research", "literature", "synthesis", "survey", "academic",
        "scientific", "knowledge_generation",
    ),
    "planning": ("planning", "design", "decompose", "schedule", "architect"),
    "implementation": (
        "coding", "implementation", "code", "write", "develop", "program",
    ),
    "verification": (
        "verification", "testing", "validate", "check", "verify", "test",
    ),
    "analysis": ("analysis", "review", "critique", "evaluate", "assess"),
    "synthesis": ("synthesis", "combine", "merge", "integrate", "summarize"),
}

CATEGORY_WORKER_TYPES = {
    "retrieval": "retrieval_worker",
    "research": "researcher",
    "planning": "planning_worker",
    "implementation": "implementation_worker",
    "verification": "verification_worker",
    "analysis": "analysis_worker",
    "synthesis": "synthesis_worker",
}


def contains_marker(text: str, markers: Iterable[str]) -> bool:
    normalized = str(text or "").lower()
    return any(marker in normalized for marker in markers)


def score_capability_categories(capabilities: Iterable[str]) -> dict[str, int]:
    scores = {category: 0 for category in CAPABILITY_CATEGORIES}
    for capability in capabilities:
        normalized = str(capability).lower()
        for category, markers in CAPABILITY_CATEGORIES.items():
            if any(marker in normalized for marker in markers):
                scores[category] += 1
    return scores

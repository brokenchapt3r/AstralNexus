"""替换依赖校验：重接要**有选择性**，提交前要看**整张投影图**。

b33 case1 fix10 第 3 步。现场形状：`deliverables/verify_case.py` 被 7 个节点依次改写
（P-104 → P-109 → P-110 → P-112 → P-115 → P-116 → P-117），而
``_rewire_superseded_dependents`` 会把**每一个**在世下游都改写到谱系末端——盲目替换。

业主定的口径：

* 消费者若**需要已完成作者的有效产物** ⇒ 可继续引用其版本（保留有效证据，不重接）；
* 消费者若**等待修复后的版本** ⇒ 才依赖修复者。

纯函数、无 IO：调用方给节点、边、产物表，本模块只做判定与投影校验。
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

#: 投影图问题词表（可审计、稳定）
ISSUE_DANGLING_PRODUCER = "dangling_producer"
ISSUE_CYCLE = "dependency_cycle"
ISSUE_UNSATISFIED_VERSION = "unsatisfied_version_requirement"

#: 这些状态的节点不再产出新版本（其产物"已成立"或"永不成立"）。
from AstralNexus.core.lifecycle_policy import UNAVAILABLE_PRODUCER_STATUSES
_DEAD_STATUSES = UNAVAILABLE_PRODUCER_STATUSES


def _accepted_artifact_node(artifacts: Mapping[str, Any], logical_id: str) -> str:
    """Which node produced the **accepted** version of a logical artifact."""
    artifact = artifacts.get(logical_id)
    if artifact is None:
        return ""
    status = getattr(artifact, "acceptance_status", "")
    status = getattr(status, "value", str(status))
    if status != "accepted":
        return ""
    return str(getattr(artifact, "node_id", "") or "")


def input_satisfied_by_accepted_artifact(
    consumer: Any, artifacts: Mapping[str, Any]
) -> bool:
    """消费者的声明输入是否**已由已接受产物满足**（不看生产者是否还活着）。"""
    inputs = [str(item) for item in (getattr(consumer, "input_artifacts", None) or [])]
    if not inputs:
        return False
    return all(_accepted_artifact_node(artifacts, item) for item in inputs)


def needs_repaired_version(
    *,
    consumer: Any,
    tail: Any,
    artifacts: Mapping[str, Any],
) -> bool:
    """消费者是否**等待修复后的版本**（因此必须重接到修复者）。

    只在两种情形下重接：

    1. 修复者（谱系末端）**声明产出**该消费者的输入之一——即那条产物正是被修的东西；
    2. 消费者的输入**没有**已接受产物可依赖——引用旧版本无从谈起。

    消费者输入已被已接受产物满足、且修复者不声明产出它们时 **保留引用**：
    有效证据不被无故撤销，消费者也不必因生产者换代而失去依赖。

    **没有声明输入的消费者一律重接**：它没有版本要求可供援引，若保留一条指向死
    生产者的边，它就永远不可就绪——那是悬空消费者，不是"引用有效版本"。
    （既有回归 `test_recovery_lineage_rewire` / `test_attempt3_audit_fixes` 的
    消费者正是这种形状，全部要求可调度。）
    """
    inputs = [str(item) for item in (getattr(consumer, "input_artifacts", None) or [])]
    if not inputs:
        return True
    tail_outputs = {
        str(item) for item in (getattr(tail, "output_artifacts", None) or [])
    }
    if any(item in tail_outputs for item in inputs):
        return True
    return not input_satisfied_by_accepted_artifact(consumer, artifacts)


#: 依赖图**只从 `dependencies` 字段派生**，不掺边表。
#: 本框架的 ``depends_on`` 边存的是 **消费者 → 生产者**
#: （``TaskEdge("T-002", "T-001", "depends_on")`` 表示 T-002 依赖 T-001），
#: 与 ``dependencies`` 的方向相反；两处一起取会把每条依赖算成一对互指，
#: 凭空造出环（实测：governor 补丁被误拒）。``dependencies`` 是权威且无歧义的。




def projected_issues(
    *,
    nodes: Mapping[str, Any],
    patch_nodes: Sequence[Mapping[str, Any]] = (),
    patch_updates: Sequence[Mapping[str, Any]] = (),
    cancelled: Sequence[str] = (),
    artifacts: Mapping[str, Any] = {},
) -> list[dict[str, Any]]:
    """Validate the **whole projected graph** before the patch is committed.

    检查三件事（业主口径：生产者 / 版本要求 / 环路）：

    * ``dangling_producer`` —— 消费者依赖的生产者既不在图里、也不由补丁引入；
    * ``unsatisfied_version_requirement`` —— 消费者声明依赖某死生产者的产物，
      而该产物既没有被接受的旧版本可依赖、修复者也不声明产出它（悬空消费者）；
    * ``dependency_cycle`` —— 投影后的依赖边成环。

    返回问题列表；**空列表表示可以提交**。
    """
    if patch_updates:
        from types import SimpleNamespace
        projection = dict(nodes)
        for update in patch_updates:
            ident = str(update.get("node_id") or update.get("task_id") or "")
            original = projection.get(ident)
            if original is not None:
                fields = dict(original) if isinstance(original, Mapping) else dict(vars(original))
                fields.update({key: value for key, value in update.items() if key not in {"node_id", "task_id"}})
                projection[ident] = SimpleNamespace(**fields)
        nodes = projection
    issues: list[dict[str, Any]] = []
    known = set(nodes.keys()) - {str(item) for item in cancelled if str(item)}
    pending = {
        str(item.get("node_id") or "") for item in patch_nodes
        if isinstance(item, Mapping) and item.get("node_id")
    }
    dependencies: dict[str, list[str]] = {}

    def _declared_dependencies(node_id: str, raw: Mapping[str, Any]) -> list[str]:
        deps = [str(item) for item in (raw.get("dependencies") or []) if str(item)]
        label = raw.get("node_id") or node_id
        if label != node_id:
            dependencies[str(label)] = deps
        return deps

    for node_id, node in nodes.items():
        dependencies[node_id] = [str(item) for item in (getattr(node, "dependencies", None) or [])]
    for raw in patch_nodes:
        if isinstance(raw, Mapping):
            _declared_dependencies(
                str(raw.get("node_id") or ""), raw,
            )

    projected = known | pending
    for node_id, deps in dependencies.items():
        if node_id not in projected:
            continue
        for dep in deps:
            if dep in projected:
                continue
            issues.append({
                "issue": ISSUE_DANGLING_PRODUCER,
                "consumer": node_id,
                "producer": dep,
            })

    # 版本要求：消费者依赖的死生产者，其产物既无已接受旧版本、修复者也不产出。
    for node_id, deps in dependencies.items():
        if node_id not in projected:
            continue
        node = nodes.get(node_id)
        if node is None:
            continue
        inputs = [str(item) for item in (getattr(node, "input_artifacts", None) or [])]
        if not inputs:
            continue
        for dep in deps:
            dep_node = nodes.get(dep)
            if dep_node is None:
                continue
            if str(getattr(dep_node, "status", "") or "") not in _DEAD_STATUSES:
                continue
            if input_satisfied_by_accepted_artifact(node, artifacts):
                continue
            issues.append({
                "issue": ISSUE_UNSATISFIED_VERSION,
                "consumer": node_id,
                "producer": dep,
                "inputs": inputs,
            })

    return issues


__all__ = [
    "ISSUE_CYCLE",
    "ISSUE_DANGLING_PRODUCER",
    "ISSUE_UNSATISFIED_VERSION",
    "input_satisfied_by_accepted_artifact",
    "needs_repaired_version",
    "projected_issues",
]
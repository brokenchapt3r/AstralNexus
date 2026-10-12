"""Development procedures selected from task intent and recorded check results.

These are planning inputs, not new permissions or acceptance criteria. Commands
come only from existing check leaves; the module never executes model text.

Artifact-class gaps (a declared file is missing or empty) have no command to
re-run: their repair is to produce the artifact the criterion names, so they get
a non-command route that carries the missing path and its owning node.
"""
from __future__ import annotations

import re
from fnmatch import fnmatchcase
from typing import Any
from AstralNexus.core.task_graph import TaskGraph
from .acceptance_checks import EXECUTION_KINDS, KIND_COMMAND_OUTPUT, EXECUTION_CRITERION_GUIDANCE, parse_criterion


def fullstack_playbook(goal: str) -> str:
    text = goal.casefold()
    if not any(word in text for word in ("全栈", "fullstack", "full-stack", "full stack")):
        return ""
    return (
        "全栈开发预定流程 fullstack/v1（阶段边界，不是固定节点数）：\n"
        "1. 契约：确定已有技术栈、接口/数据模型、启动命令、测试命令和交付验收项；"
        "复用已有项目，不为调查而重建架构。\n"
        "2. 最小纵向功能：数据库→后端接口→前端操作，先让一个真实用户流程贯通；"
        "按文件责任分工，其余独立功能可并行。\n"
        "3. 集成：明确服务启动者、健康检查、端口及测试数据生命周期；服务未就绪先修环境，"
        "不要生成业务失败报告。验证消费者完成前保留其需要的服务。\n"
        "4. 实测：先执行项目已有测试，再验证真实 HTTP 和浏览器操作；"
        "优先用 runtime_dependencies 确认运行环境、http_request 获取真实状态码与响应、"
        "sqlite_query 只读核对数据；浏览器交互通过已配置 MCP，缺提供方应明确报告缺口，"
        "不能把 http_request 或 browser_fetch 当成浏览器操作验证。"
        "检查摘要格式、退出码、收据和产物版本，不能只打印通过文字。\n"
        + EXECUTION_CRITERION_GUIDANCE + "\n" +
        "5. 定点修复：沿同一验收项执行确认前提→修改责任文件→原命令复验；"
        "已明确缺口不得重新扩成全项目调查。业务失败修实现；摘要缺失修采集器；"
        "证据过期重新执行。修改后回到受影响测试，保留不受影响证据。\n"
        "6. 交付：必需验收项闭合后再生成最终报告、清单和发布物；"
        "报告装配成功不能替代功能验收。每阶段以实际文件/收据进入下一阶段，"
        "不得因更换节点名视为取得进展。\n"
    )


_REPAIRS = {
    "COMMAND_OUTPUT_MISMATCH": (
        "repair_implementation", "对照冻结的预期输出定位实现错误；不得修改预期值或追加测试摘要改变业务输出。",
    ),
    "COMMAND_EXIT_CODE_UNAVAILABLE": (
        "repair_execution", "补齐真实命令退出码采集；没有退出码不能推定成功。",
    ),
    "COMMAND_OUTPUT_UNAVAILABLE": (
        "repair_test_adapter", "回读本次真实执行的完整输出；截断尾部不能证明精确输出，不改写历史收据。",
    ),
    "TEST_SUMMARY_NOT_RECOGNIZED": (
        "repair_test_adapter",
        "核对原执行输出；修测试脚本或采集器，让真实断言结果输出受支持摘要；不得只追加成功文字。",
    ),
    "TEST_EXECUTION_NOT_FOUND": (
        "execute_missing_check", "确认服务和工作目录就绪，执行原验收命令并记录收据。",
    ),
    "TEST_EXECUTION_STALE": (
        "rerun_after_change", "确认当前代码及输入版本，重新执行原命令，不重写历史收据。",
    ),
    "TEST_EXECUTION_FAILED": (
        "repair_execution", "读取锚定命令的错误；区分环境错误与实现错误，交给对应文件所有者修改后复验。",
    ),
    # Fix r29 (doc 51 §16): the anchored command was REFUSED by the loop guard,
    # so no execution result exists at all.  The repair is not to weaken anchoring
    # and not to reword the command: confirm which tracked premise file changed,
    # then let the framework admit one re-verification of the ORIGINAL command.
    "TEST_EXECUTION_BLOCKED": (
        "rerun_after_change",
        "被拦调用不是执行结果：核对该验收项声明的被跟踪文件版本是否已变化；"
        "变化后按原命令复验并保留新收据，不得改写命令文字绕过守卫，也不得把被拦回执当通过。",
    ),
    "TESTS_REPORTED_FAILURES": (
        "repair_implementation", "定位失败断言对应实现，修责任文件后运行原测试；不得以改报告代替修复。",
    ),
    "TEST_SUMMARY_ZERO_TESTS": (
        "repair_test_collection", "修测试发现路径/选择条件，确认真正执行了要求的用例后复验。",
    ),
}


#: Fix F1 (doc 51 §29): a deterministic artifact-class failure made the recovery
#: handoff carry ``repair_procedures: []``.  Live r31 evidence: N-102 failed
#: ``N-102.AC01/AC02/EO01 [FILE_NOT_FOUND]`` and the planner, with no route to
#: produce the missing file, could only propose reopening the same node.  The
#: anchored criterion is a file question, not a test question, so there is no
#: command to replay and the repair must not pretend otherwise.
_ARTIFACT_ROUTE = "produce_missing_artifact"

_ARTIFACT_REASONS = frozenset({
    "FILE_NOT_FOUND",
    "FILE_EMPTY",
    "FILE_SIZE_UNKNOWN",
    "FILE_CHECK_IO_ERROR",
    "FILE_NOT_IN_SHARED_DOMAIN",
    "WORKSPACE_UNAVAILABLE",
})

#: The artifact-class criterion grammar, kept in step with the deterministic
#: checks in ``runtime/acceptance_checks.py`` (``_FILE_WRITTEN_RE``,
#: ``_FILE_EXISTS_RE``, ``_FILE_NON_EMPTY_RE``).  Report-class criteria
#: (``report_not_stale:``, ``sha_matches:``) are deliberately absent: a report
#: gap keeps its own handling and is never re-framed as an implementation gap.
_ARTIFACT_CRITERIA = (
    re.compile(r"^file_written:(?P<path>.+?)$", re.IGNORECASE),
    re.compile(r"^file:(?P<path>.+?)\s+exists$", re.IGNORECASE),
    re.compile(r"^file:(?P<path>.+?)\s+non-?empty$", re.IGNORECASE),
)

#: A superseded/cancelled/failed node still *names* a path but cannot produce it.
#: Live r31 cancelled N-103 (``deliverables/runs/env/``) as a contract-free
#: receipt chain, so its ownership must never be re-proposed as the producer.
from AstralNexus.core.lifecycle_policy import REPAIR_SUCCESSOR_STATUSES
_INACTIVE_OWNERS = REPAIR_SUCCESSOR_STATUSES


def _normalized_path(value: Any) -> str:
    return str(value or "").strip().replace("\\", "/").strip("/")


def artifact_paths(criterion: Any) -> list[str]:
    """The artifact target named by an artifact-class criterion, else ``[]``."""
    text = str(criterion or "").strip()
    for pattern in _ARTIFACT_CRITERIA:
        match = pattern.match(text)
        if match:
            path = _normalized_path(match.group("path"))
            if path:
                return [path]
    return []


def artifact_owner(path: Any, nodes: Any, fallback_node_id: str = "") -> tuple[str, str, list[str]]:
    """Which node's declared ownership covers a missing artifact path.

    Returns ``(owner, owner_status, inactive_owners)``.  An active owner wins the
    most specific declaration (longest owned path).  When only inactive nodes
    declare the path, ``fallback_node_id`` — the node whose acceptance failed — is
    reported as the owner *together with its status*, so a superseded or cancelled
    lineage is never re-proposed as a live producer without saying so.  The
    remaining inactive declarers are reported so the planner does not route the
    work back into a cancelled or superseded chain.
    """
    target = _normalized_path(path)
    if not target:
        return "", "", []
    active: list[tuple[int, str, str]] = []
    inactive: list[str] = []
    statuses: dict[str, str] = {}
    for node in nodes or []:
        node_id = str(getattr(node, "node_id", "") or "")
        if not node_id:
            continue
        status = str(getattr(node, "status", "") or "")
        statuses[node_id] = status
        for owned in getattr(node, "owned_paths", []) or []:
            prefix = _normalized_path(owned)
            if not prefix:
                continue
            covered = (target == prefix or target.startswith(prefix + "/")
                       or (any(char in prefix for char in "*?[")
                           and fnmatchcase(target, prefix)))
            if not covered:
                continue
            if status in _INACTIVE_OWNERS:
                if node_id not in inactive:
                    inactive.append(node_id)
            else:
                active.append((len(prefix), node_id, status))
    if active:
        owner, owner_status = sorted(
            active, key=lambda item: (-item[0], item[1]))[0][1:]
    else:
        owner = str(fallback_node_id or "")
        owner_status = statuses.get(owner, "")
    return owner, owner_status, [node_id for node_id in inactive if node_id != owner][:4]


def recovery_procedures(
    observations: Any, *, owner_node_id: str = "", nodes: Any = None
) -> list[dict[str, Any]]:
    """Preserve check identity and exact recheck command across recovery hops.

    ``owner_node_id`` is the node whose acceptance failed (the fallback producer)
    and ``nodes`` supplies declared ownership for the artifact route.
    """
    if not isinstance(observations, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for item in observations:
        if not isinstance(item, dict) or (item.get("checked") is True and item.get("holds") is True):
            continue
        criterion = str(item.get("criterion") or "")
        criterion_id = str(item.get("criterion_id") or "")
        reason = str(item.get("reason") or "")
        paths = artifact_paths(criterion)
        if reason in _ARTIFACT_REASONS and paths:
            owner, owner_status, inactive = artifact_owner(
                paths[0], nodes, fallback_node_id=str(owner_node_id or ""))
            producer = owner or "修复节点"
            if owner and owner_status in _INACTIVE_OWNERS:
                producer = "%s（状态 %s，已失效：必须由其修复/继任节点承担）" % (
                    owner, owner_status)
            identity = (str(item.get("execution_id") or ""), criterion_id, criterion, reason)
            if identity in seen:
                continue
            seen.add(identity)
            result.append({
                "criterion_id": criterion_id,
                "criterion": criterion,
                "source_execution_id": identity[0],
                "reason": reason,
                "route": _ARTIFACT_ROUTE,
                "missing_paths": paths,
                "owner": owner,
                "owner_status": owner_status,
                "inactive_owners": inactive,
                "steps": [
                    "确认 missing_paths 在最终工作区内的目标路径与当前责任节点",
                    "由 %s 真实生成该缺失产物；计划文本、报告或占位文件不算产物" % producer,
                    "让 %s 的确定性检查重新执行，取得新的 checked/holds 叶子" % criterion_id,
                    "只补缺失产物：已存在且有效的证据不得重写，也不得因本缺口作废",
                ],
                "recheck_command": "",
                "recheck": "deterministic_check:%s" % criterion_id,
                "completion": "同一 criterion 的新检查叶子 checked=true 且 holds=true，"
                              "且 missing_paths 在工作区内真实存在；调查、报告或改名不算完成",
            })
            continue
        kind, payload = parse_criterion(criterion)
        if reason not in _REPAIRS or kind not in EXECUTION_KINDS:
            continue
        if kind == KIND_COMMAND_OUTPUT and payload.get("invalid"):
            continue
        command = payload["command"] if kind == KIND_COMMAND_OUTPUT else str(payload)
        test_identity = (str(item.get("execution_id") or ""), criterion, reason)
        if not command or test_identity in seen:
            continue
        seen.add(test_identity)
        route, action = _REPAIRS[reason]
        result.append({
            "criterion_id": criterion_id,
            "criterion": criterion,
            "source_execution_id": test_identity[0],
            "reason": reason,
            "route": route,
            "steps": ["针对当前证据确认前提", action, "运行原命令复验", "按新收据更新该验收项"],
            "recheck_command": command,
            "completion": "同一 criterion 的新执行收据经独立检查 checked=true 且 holds=true；调查或报告不算完成",
        })
    return result


def validate_replacement_rechecks(graph: TaskGraph, patch: Any) -> None:
    """A replacement must not silently erase a known executable test gap.

    Only bind existing criteria with a definite repair route. A repair producer
    may delegate the recheck to a dependent node; unrelated tests cannot count.
    Validate a projection before the graph mutation, never edit live state.
    """
    projected: dict[str, dict[str, Any]] = {
        node.node_id: dict(status=node.status, dependencies=list(node.dependencies),
            acceptance_criteria=list(node.acceptance_criteria), metadata=dict(node.metadata))
        for node in graph.nodes.values()
    }
    for raw in [*patch.add_nodes, *patch.update_nodes]:
        if not isinstance(raw, dict):
            continue  # Existing structural validator reports malformed patches.
        key = str(raw.get('node_id') or '')
        old = projected.get(key, {})
        projected[key] = {**old, **raw, 'metadata': {
            **(old.get('metadata') or {}), **(raw.get('metadata') or {})}}
    for key in patch.cancel_nodes:
        if key in projected:
            projected[key] = {**projected[key], 'status': 'cancelled'}
    inactive = {'superseded', 'cancelled', 'failed', 'completed'}
    for source in graph.nodes.values():
        if source.status in {'superseded', 'cancelled'}:
            continue
        if projected[source.node_id].get('status') not in {'superseded', 'cancelled'}:
            continue
        receipt = source.metadata.get('_verification_receipt') or {}
        if not isinstance(receipt, dict):
            continue
        required = {
            item['criterion'] for item in recovery_procedures(receipt.get('observations'))
            if item['reason'] in {'TEST_SUMMARY_NOT_RECOGNIZED', 'TEST_EXECUTION_STALE', 'TESTS_REPORTED_FAILURES', 'TEST_EXECUTION_BLOCKED'}
            and item['criterion'] in source.acceptance_criteria
        }
        if not required:
            continue
        # Follow explicit lineage and data dependencies to support repair→test.
        reachable = {source.node_id}
        while True:
            added = {
                key for key, item in projected.items()
                if key not in reachable and (
                    any(dep in reachable for dep in (item.get('dependencies') or []))
                    or any((item.get('metadata') or {}).get(field) in reachable
                           for field in ('recovery_of', 'supersedes_node_id')))
            }
            if not added:
                break
            reachable.update(added)
        carried = {
            criterion for key in reachable if key != source.node_id
            and projected[key].get('status') not in inactive
            for criterion in (projected[key].get('acceptance_criteria') or [])
        }
        missing = required - carried
        if missing:
            raise ValueError(
                f'RECOVERY_RECHECK_DROPPED: {source.node_id}: retain {sorted(missing)} '
                'on the replacement or its dependent recheck node; investigation/report alone cannot close the gap'
            )

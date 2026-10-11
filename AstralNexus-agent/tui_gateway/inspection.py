"""Read-only user views of the existing authoritative run and public evidence."""
from __future__ import annotations
import json
from collections import Counter, deque
from pathlib import Path
from typing import Any
from AstralNexus.runtime.authoritative_projection import read_current_projection
from AstralNexus.core.publication_validation import inspect_publication


def inspect_session(session: Any, kind: str) -> dict[str, Any]:
    if kind not in {'plan', 'evidence', 'delivery', 'changes'}:
        raise ValueError('unsupported inspection view')
    lines = [f'# {session.title}', f'工作区：{session.workspace}']
    if kind == 'changes':
        lines.append('文件工具与同步命令的工作区变更；后台进程或外部编辑器的后续修改不作为 Agent 变更归因。')
        for change in session.review.changes():
            lines += [f"{change['id']} · {change['path']} · 工具状态 {change['tool_status']} · 已撤销 {change['restored']} · 用户审查 {change['review_decision']}",
                      f"版本：{change['before_sha']} → {change['after_sha']}", change['diff']]
        lines.append('停止任务后，使用 /accept <change-id> 接受此文件版本，/reject 或 /restore <change-id> 撤销；文件后续有修改时拒绝覆盖。历史收据不会删除。')
        return {'kind': kind, 'text': '\n'.join(lines)}
    if not session.run_dir:
        return {'kind': kind, 'text': '\n'.join(lines + ['尚未绑定正式运行；没有推断或选择其他会话的运行。'])}
    root = Path(session.run_dir)
    projection = read_current_projection(root)
    lines += [f'运行：{root}', f'权威状态：{projection.get("run_status", "unknown")}']
    if not projection.get('verified'):
        return {'kind': kind, 'text': '\n'.join(lines + [f'当前图不可确认：{projection.get("reason", "尚无权威检查点")}'])}
    graph = projection['graph']
    if projection.get('generation_alignment') == 'pending_generation':
        lines.append('当前管理器已接管，但新代数尚未提交图：下面是上一代的验收快照，不能当成本代新进展。')
    nodes = graph.get('nodes', [])
    counts = dict(Counter(node['status'] for node in nodes))
    lines.append('节点状态（不是最终交付结论）：' + json.dumps(counts, ensure_ascii=False))
    dependencies: dict[str, list[str]] = {node['node_id']: [] for node in nodes}
    for edge in graph.get('edges', []):
        if edge.get('edge_type') == 'depends_on' and edge['target'] in dependencies:
            dependencies[edge['target']].append(edge['source'])
    lines += [f'代数：{projection["generation"]}；提交：{projection["commit_seq"]}；图版本：{projection["cursor"].get("graph_revision", "未知")}']
    if kind == 'plan':
        lines += ['节点完成只代表节点验收；最终交付门单独显示。', '']
        for node in graph.get('nodes', []):
            meta = node.get('metadata') or {}
            deps = dependencies[node['node_id']]
            lines += [f"[{node['status']}] {node['node_id']} · {node.get('goal', '')}",
                      f"  依赖：{', '.join(deps) or '无'}；恢复来源：{meta.get('recovery_of') or '原计划'}"]
            for key in ('readiness_blockers', 'last_failure', 'verification_rejection'):
                if meta.get(key): lines.append(f'  {key}：{json.dumps(meta[key], ensure_ascii=False)}')
        lines.append('最长依赖链（节点数，不是工时预测）：' + dependency_chain(nodes, dependencies))
        cursor = projection.get('cursor') or {}
        lines.append('运行预算/恢复账本：' + json.dumps(cursor.get('recovery_cost_ledger') or {
            key: value for key, value in cursor.items() if 'budget' in key or 'churn' in key}, ensure_ascii=False))
        lines.append('谱系成本（不与恢复阶段事件数相加）：' + json.dumps(cursor.get('lineage_accounting') or {}, ensure_ascii=False))
    elif kind == 'evidence':
        lines.append('产物与证据（身份/版本/验收状态来自图；路径存在不代表通过）：')
        artifacts = graph.get('artifacts', [])
        if isinstance(artifacts, dict): artifacts = list(artifacts.values())
        for artifact in artifacts:
            state = str(artifact.get('acceptance_status') or 'unknown')
            label = {'accepted': '已采用（检查点验收）', 'rejected': '已拒绝', 'pending': '尚未验收'}.get(state, '状态未知')
            lines.append(f"{label} · {artifact.get('path', '')} · {artifact.get('artifact_id', '')} · 来源节点 {artifact.get('node_id') or 'run'}")
            if artifact.get('description'): lines.append(str(artifact['description']))
        lines.append('\n未闭合的判据与公开验证依据：')
        for node in nodes:
            metadata = node.get('metadata') or {}
            receipt = metadata.get('_verification_receipt') or {}
            if isinstance(receipt, dict) and receipt:
                lines.append(f"{node['node_id']} · 验证结论 {receipt.get('verdict', 'unknown')}")
                for observation in receipt.get('observations') or []:
                    lines.append('  验证观察：' + json.dumps(observation, ensure_ascii=False))
                for failure in receipt.get('failed_dimensions') or []:
                    lines.append('  未通过：' + json.dumps(failure, ensure_ascii=False))
            for key in ('last_failure', 'verification_rejection', 'readiness_blockers'):
                if metadata.get(key): lines.append(f"{node['node_id']} · {key}：" + json.dumps(metadata[key], ensure_ascii=False))
        lines.append('未记录采用理由或信息缺口时不推断；这些是公开验收记录，不是模型私有思维链。')
        lines.append('\n已公开的信息获取记录：')
        after = 0
        while session.store is not None:
            events = session.store.events(session.session_id, after, 1000)
            if not events: break
            for event in events:
                payload = event['payload']
                if event['kind'] == 'tool.start':
                    lines.append(f"{payload.get('node_id', '')} · {payload.get('name', '')} · {payload.get('args_text', '')}")
            after = events[-1]['seq']
    else:
        candidates = []
        for manifest in (root / 'deliverables').glob('v*/delivery_manifest.json'):
            candidates.append(manifest.parent / 'delivery_summary.json')
        if not candidates:
            lines.append('尚无正式发布版本；不能由节点完成数判定交付通过。')
        for summary in sorted(candidates):
            verdict = inspect_publication(root, {'summary_json': str(summary)})
            label = '完整验收并发布' if verdict.get('complete') else '已发布但有未闭合验收项' if verdict.get('published') else '发布产物不可确认'
            lines.append(f'{summary.parent.name}：{label}')
            lines.append(json.dumps(verdict, ensure_ascii=False))
            if verdict.get('published'):
                gate = verdict['gate']
                checks = gate.get('criterion_results') or []
                lines.append('最终门判据（不冒充 pytest 用例数量）：')
                for check in checks:
                    lines.append(f"[{check.get('status', 'unknown')}] {check.get('criterion_id', '')} · {check.get('description') or check.get('name') or ''}")
                    lines.append(str(check.get('explanation') or check.get('reason') or ''))
                lines.append('失败项：' + json.dumps(gate.get('failed_tests') or [], ensure_ascii=False))
                lines.append('未知项：' + json.dumps(gate.get('unknown_tests') or [], ensure_ascii=False))
        changes = session.review.changes()
        lines.append('会话已观察到的文件变更：' + str(sum(bool(change['changed']) for change in changes)))
        lines.append('动作：/changes 查看差异；停止后 /restore <change-id> 撤销文件；/plan 查看未完成节点；版本目录可直接检查产物。')
    return {'kind': kind, 'text': '\n'.join(lines)}


def dependency_chain(nodes: list[dict[str, Any]], dependencies: dict[str, list[str]]) -> str:
    """Linear DAG projection; cycles/missing endpoints remain explicit unknowns."""
    pending = {node['node_id']: len(dependencies[node['node_id']]) for node in nodes}
    children: dict[str, list[str]] = {key: [] for key in pending}
    for target, parents in dependencies.items():
        for parent in parents:
            if parent not in children: return '未知：依赖端点缺失'
            children[parent].append(target)
    ready = deque(key for key, count in pending.items() if count == 0)
    distances: dict[str, int] = {}
    predecessors: dict[str, str | None] = {}
    processed = 0
    while ready:
        key = ready.popleft()
        parents = dependencies[key]
        predecessor = max(parents, key=lambda parent: distances[parent], default=None)
        predecessors[key] = predecessor
        distances[key] = (distances[predecessor] if predecessor is not None else 0) + 1
        processed += 1
        for child in children[key]:
            pending[child] -= 1
            if pending[child] == 0: ready.append(child)
    if processed != len(nodes): return '未知：依赖包含环'
    current = max(distances, key=lambda key: distances[key], default=None)
    chain = []
    while current is not None:
        chain.append(current)
        current = predecessors[current]
    return ' → '.join(reversed(chain)) or '无'

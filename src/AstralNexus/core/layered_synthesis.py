"""Governor-controlled layered evidence synthesis.

Raw node outputs stay immutable.  The final synthesizer receives only the
Governor-approved structured evidence workset and the pre-final verifier
report; it never receives the full node transcript by default.
"""

from __future__ import annotations
from AstralNexus.runtime.run_context import get_env

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Protocol


class SynthesisChatClient(Protocol):
    def chat(self, messages: list[dict[str, str]], *, max_tool_rounds: int | None = None) -> str: ...

from AstralNexus.runtime.llm_client import LLMClient
from AstralNexus.core.output_paths import RunOutputLayout


def _parse_json(text: str) -> Any:
    value = str(text or "").strip()
    if value.startswith("```"):
        value = value.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"(\{.*\}|\[.*\])", value, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else ([] if value in (None, "") else [value])


def _infer_confidence(text: str, explicit: Any = None) -> float:
    """Heuristic confidence from text quality when no explicit value is provided."""
    if explicit is not None:
        try:
            v = float(explicit)
            if 0.0 < v <= 1.0:
                return v
        except (TypeError, ValueError):
            pass
    length_score = min(len(text) / 2000.0, 0.8)
    structure_score = 0.1 * (text.count('\n') > 5) + 0.05 * ('：' in text or ':' in text)
    return round(max(0.3, min(length_score + structure_score, 0.95)), 2)


def _infer_status(text: str, explicit: Any = None) -> str:
    """Heuristic epistemic_status from content signals when no explicit value is provided."""
    _valid = {"observed", "derived", "hypothesis", "planned", "unknown"}
    if explicit and str(explicit) in _valid:
        return str(explicit)
    text_lower = text.lower()
    if any(w in text_lower for w in ["实测", "观测", "实验结果", "measured", "observed", "experimental"]):
        return "observed"
    if any(w in text_lower for w in ["假设", "推测", "可能", "hypothesis", "assume", "might"]):
        return "hypothesis"
    if any(w in text_lower for w in ["计划", "待实现", "planned", "todo", "will"]):
        return "planned"
    return "derived"


def _load_packets(run_dir: Path, *, task_version: int = 1, graph: Any = None) -> list[dict[str, Any]]:
    """Build non-empty, traceable L1 EvidencePackets from node artifacts."""
    packets: list[dict[str, Any]] = []
    from AstralNexus.runtime.evidence_catalog import EvidenceCatalog
    catalog = EvidenceCatalog(run_dir, graph=graph, task_version=task_version)
    latest: dict[str, tuple[tuple, Path, dict[str, Any]]] = {}
    for path in sorted((run_dir / "node_io").glob("*_output.json")):
        try:
            payload = json.loads(catalog.read_text(path) or "{}")
        except (OSError, json.JSONDecodeError):
            continue
        node_id = str(payload.get("node_id") or path.stem)
        if graph is not None and not catalog.validate_reference(path, source_nodes={node_id})["valid"]:
            continue
        key = (int(payload.get("task_version") or task_version), str(payload.get("recorded_at") or ""), int(payload.get("attempt") or 1), path.name)
        if node_id not in latest or key > latest[node_id][0]:
            latest[node_id] = (key, path, payload)
    for _, path, payload in latest.values():
        if str(payload.get("role", "")).lower() in {"planner", "governor", "user_intent"}:
            continue
        if int(payload.get("task_version") or task_version) != task_version:
            continue
        if payload.get("execution_outcome") != "succeeded":
            continue
        input_path = path.with_name(path.name.replace("_output.json", "_input.json"))
        try:
            input_payload = json.loads(catalog.read_text(input_path) or "{}")
        except (OSError, json.JSONDecodeError):
            input_payload = {}
        content = str(payload.get("content", "")).strip()
        if not content:
            continue
        embedded = payload.get("evidence_packet")
        if not isinstance(embedded, dict):
            parsed = _parse_json(content)
            embedded = parsed if isinstance(parsed, dict) else {}
        node_id = str(payload.get("node_id") or path.stem)
        conclusion = str(embedded.get("conclusion") or embedded.get("result") or content).strip()
        
        claims = _as_list(embedded.get("claims") or embedded.get("verified_claims"))
        normalized_claims = []
        for claim in claims:
            if isinstance(claim, dict):
                claim_text = str(claim.get("claim") or claim.get("text") or claim.get("statement") or "").strip()
                if not claim_text:
                    continue
                normalized_claims.append({
                    "claim": claim_text,
                    "evidence_refs": _as_list(claim.get("evidence_refs")) or [f"node_io/{path.name}"],
                    "confidence": _infer_confidence(claim_text, claim.get("confidence", embedded.get("confidence"))),
                    "epistemic_status": _infer_status(claim_text, claim.get("epistemic_status") or embedded.get("epistemic_status")),
                })
            elif str(claim).strip():
                claim_text = str(claim).strip()
                normalized_claims.append({
                    "claim": claim_text,
                    "evidence_refs": [f"node_io/{path.name}"],
                    "confidence": _infer_confidence(claim_text, embedded.get("confidence")),
                    "epistemic_status": _infer_status(claim_text, embedded.get("epistemic_status")),
                })
        if not normalized_claims:
            # Extract key sentences instead of copying entire conclusion
            sentences = [s.strip() for s in conclusion.replace('\n', '。').split('。') if len(s.strip()) > 20][:3]
            if sentences:
                normalized_claims = [{
                    "claim": sent,
                    "evidence_refs": [f"node_io/{path.name}"],
                    "confidence": _infer_confidence(sent, embedded.get("confidence")),
                    "epistemic_status": _infer_status(sent, embedded.get("epistemic_status")),
                } for sent in sentences]
            else:
                normalized_claims = [{
                    "claim": conclusion[:200],
                    "evidence_refs": [f"node_io/{path.name}"],
                    "confidence": _infer_confidence(conclusion, embedded.get("confidence")),
                    "epistemic_status": _infer_status(conclusion, embedded.get("epistemic_status")),
                }]
        execution_id = str(payload.get("execution_id") or path.stem)
        attempt = int(payload.get("attempt") or 1)
        runtime_evidence: dict[str, Any] = {
            "source": "real_llm_node_output", "raw_artifact": f"node_io/{path.name}",
        }
        node = graph.get_node(node_id) if graph is not None else None
        runtime_id = str(node.metadata.get("runtime_execution_id") or "") if node is not None else ""
        if node is not None and runtime_id and (run_dir / ".astralnexus_state" / "executions.sqlite").is_file():
            from AstralNexus.runtime.execution_store import ExecutionStore
            state = ExecutionStore(run_dir).get(runtime_id)
            acceptance = state.get("acceptance") or {}
            if isinstance(acceptance, dict) and acceptance.get("approved") is True:
                runtime_evidence = {
                    "source": "execution_store", "execution_id": runtime_id,
                    "runtime_status": state.get("status"), "acceptance": state.get("acceptance"),
                    "observations": (node.metadata.get("_verification_receipt") or {}).get("observations", []),
                    "receipts": [{key: receipt.get(key) for key in (
                        "receipt_id", "execution_id", "tool", "arguments", "status", "returncode",
                        "stdout_tail", "stderr_tail", "files", "ownership",
                    )} for receipt in state.get("receipts", [])[-50:]],
                    "receipt_count": len(state.get("receipts", [])),
                    "receipts_truncated": len(state.get("receipts", [])) > 50,
                }
        contents = []
        for ref in _as_list(payload.get("artifacts")):
            if not isinstance(ref, str):
                continue
            validation = catalog.validate_reference(ref, source_nodes={node_id})
            if not validation.get("valid"):
                continue
            text = catalog.read_text(ref, max_bytes=12000, errors="replace")
            if text is not None:
                contents.append({"path": ref, "content_excerpt": text, "byte_cap": 12000})
        runtime_evidence["verified_artifact_contents"] = contents
        packets.append({
            "packet_id": f"packet:{node_id}:v{task_version}:a{attempt}:{execution_id}",
            "task_id": node_id,
            "node_id": node_id,
            "execution_id": execution_id,
            "attempt": attempt,
            "goal": str(payload.get("goal") or payload.get("objective") or input_payload.get("goal") or f"Execute {node_id}"),
            "role": str(payload.get("role", "")),
            "status": payload.get("status"),
            "conclusion": conclusion[:5000],
            "claims": normalized_claims,
            "evidence_refs": [f"node_io/{path.name}"],
            "artifact_refs": _as_list(embedded.get("artifact_refs")) or _as_list(payload.get("artifacts")) or [f"node_io/{path.name}"],
            "limitations": _as_list(embedded.get("limitations")) or ["节点结果尚未经过 Global Verifier 独立复核"],
            "open_questions": _as_list(embedded.get("open_questions")),
            "constraints": _as_list(embedded.get("constraints") or embedded.get("constraints_preserved")),
            "confidence": _infer_confidence(conclusion, embedded.get("confidence")),
            "token_size": max(1, len(content) // 4),
            "task_version": task_version,
            "epistemic_status": _infer_status(conclusion, embedded.get("epistemic_status")),
            "lifecycle_class": str(embedded.get("lifecycle_class") or payload.get("lifecycle_class") or "durable"),
            "runtime_evidence": runtime_evidence,
        })
    return packets


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def _normalize_coverage_gaps(value: Any) -> list[dict[str, Any]]:
    """Normalize reducer gaps while preserving the delivery-critical bit.

    A reducer may discover useful open questions that are not required to
    authorize the current delivery.  The explicit flag prevents the Global
    Verifier from treating every research opportunity as a blocking defect.
    Legacy string gaps remain blocking until a model supplies the flag.
    """
    normalized: list[dict[str, Any]] = []
    for item in _as_list(value):
        if isinstance(item, dict):
            gap = str(item.get("gap") or item.get("description") or item.get("item") or "").strip()
            if not gap:
                continue
            required = item.get("required_for_delivery")
            if isinstance(required, str):
                required = required.strip().lower() in {"true", "1", "yes", "是"}
            blocking_stage = str(item.get("blocking_stage") or item.get("stage") or "evidence").strip().lower()
            if blocking_stage not in {"evidence", "synthesis", "delivery"}:
                blocking_stage = "evidence"
            normalized.append({
                "gap": gap,
                "severity": str(item.get("severity") or "major"),
                "required_for_delivery": bool(required) if isinstance(required, bool) else True,
                "blocking_stage": blocking_stage,
                "evidence_needed": str(item.get("evidence_needed") or "").strip(),
                "recommended_action": str(item.get("recommended_action") or "").strip(),
            })
        elif str(item).strip():
            normalized.append({
                "gap": str(item).strip(),
                "severity": "major",
                "required_for_delivery": True,
                "blocking_stage": "evidence",
                "evidence_needed": "",
                "recommended_action": "",
            })
    return normalized


def _normalize_reducer(group: dict[str, Any], raw: Any, packets: list[dict[str, Any]], *, task_version: int) -> dict[str, Any]:
    """Normalize model JSON without turning Markdown into a second protocol."""
    value = raw if isinstance(raw, dict) else {}
    source_refs = list(dict.fromkeys(str(ref) for packet in packets for ref in (packet.get("evidence_refs") or []) if str(ref).strip()))
    source_nodes = [str(x) for x in group.get("node_ids", [])]
    claims = value.get("verified_claims", value.get("claims", []))
    verified_claims = []
    for claim in _as_list(claims):
        if isinstance(claim, dict):
            text = str(claim.get("claim") or claim.get("text") or "").strip()
            if text:
                verified_claims.append({
                    "claim": text,
                    "evidence_refs": _as_list(claim.get("evidence_refs")) or list(source_refs),
                    "confidence": _infer_confidence(text, claim.get("confidence")),
                    "epistemic_status": _infer_status(text, claim.get("epistemic_status")),
                })
        elif str(claim).strip():
            text = str(claim).strip()
            verified_claims.append({
                "claim": text,
                "evidence_refs": list(source_refs),
                "confidence": _infer_confidence(text),
                "epistemic_status": _infer_status(text),
            })
    if not verified_claims:
        source_text = " ".join(str(p.get("conclusion", ""))[:800] for p in packets)
        if source_text.strip():
            sentences = [s.strip() for s in source_text.replace('\n', '。').split('。') if len(s.strip()) > 20][:3]
            if sentences:
                verified_claims = [{"claim": s, "evidence_refs": list(source_refs), "confidence": _infer_confidence(s), "epistemic_status": _infer_status(s)} for s in sentences]
            else:
                verified_claims = [{"claim": source_text[:3000], "evidence_refs": list(source_refs), "confidence": _infer_confidence(source_text), "epistemic_status": _infer_status(source_text)}]
        else:
            verified_claims = [{"claim": "No extractable claims from source", "evidence_refs": list(source_refs), "confidence": 0.3, "epistemic_status": "unknown"}]
    result = {
        "group_id": str(value.get("group_id") or group.get("group_id")),
        "source_nodes": source_nodes,
        "verified_claims": verified_claims,
        "conflicts": _as_list(value.get("conflicts")),
        "coverage_gaps": _normalize_coverage_gaps(value.get("coverage_gaps") or value.get("open_issues")),
        "constraints": _as_list(value.get("constraints")),
        "limitations": _as_list(value.get("limitations")),
        "open_questions": _as_list(value.get("open_questions")),
        "recommended_followups": _as_list(value.get("recommended_followups") or value.get("suggested_followups")),
        "source_artifacts": list(source_refs),
        # Context references are not evidence produced by this reducer group.
        "context_artifact_refs": _as_list(value.get("source_artifacts") or value.get("artifacts")),
        "task_version": int(value.get("task_version") or task_version),
    }
    # Model summaries cannot drop or replace the original execution facts.
    result["recorded_execution_evidence"] = [packet["runtime_evidence"]
        for packet in packets if packet.get("runtime_evidence", {}).get("source") == "execution_store"]
    return result


def _reducer_client(client: SynthesisChatClient) -> SynthesisChatClient:
    """Give each parallel reducer a bounded control-plane timeout.

    Final synthesis may legitimately need a long timeout, but a reducer is a
    bounded JSON transformation.  Sharing the final-writer timeout here can
    make one stalled API request hold the whole reducer wave indefinitely.
    """
    timeout = max(15, int(get_env("ASTRALNEXUS_REDUCER_TIMEOUT", "180")))
    retries = max(0, int(get_env("ASTRALNEXUS_REDUCER_RETRIES", "1")))
    if not isinstance(client, LLMClient):
        return client
    if getattr(client, "timeout", timeout) <= timeout and getattr(client, "max_retries", retries) <= retries:
        return client
    reduced = LLMClient(
        model=client.model,
        api_key=client.api_key,
        base_url=client.base_url,
        max_tokens=getattr(client, "max_tokens", None),
        temperature=getattr(client, "temperature", None),
        timeout=timeout,
        interactive=False,
    )
    reduced.max_retries = retries
    return reduced


def _synthesis_client(client: SynthesisChatClient) -> SynthesisChatClient:
    """Create a bounded text-only client for outline/final synthesis.

    Reducers and final writers transform already collected evidence; they do
    not discover evidence or execute tools.  Keeping their timeout/retry
    budget separate prevents a stalled provider request from consuming the
    whole orchestration run, and avoids accidentally entering a tool loop.
    """
    timeout = max(30, int(get_env("ASTRALNEXUS_SYNTHESIS_STAGE_TIMEOUT", "180")))
    retries = max(0, int(get_env("ASTRALNEXUS_SYNTHESIS_STAGE_RETRIES", "1")))
    if not isinstance(client, LLMClient):
        return client
    bounded = LLMClient(
        model=client.model,
        api_key=client.api_key,
        base_url=client.base_url,
        max_tokens=getattr(client, "max_tokens", None),
        temperature=getattr(client, "temperature", None),
        timeout=timeout,
        interactive=False,
    )
    bounded.max_retries = retries
    return bounded


def reduce_evidence_group(
    group: dict[str, Any], source_packets: list[dict[str, Any]], *,
    task_content: str, delivery_contract: dict[str, Any],
    client: SynthesisChatClient, task_version: int, governor: Any,
) -> tuple[str, dict[str, Any]]:
    """One production reducer boundary, also callable for isolated API replay."""
    source = json.dumps(source_packets, ensure_ascii=False)
    canonical_refs = [ref for packet in source_packets for ref in packet.get("evidence_refs", [])]
    reducer_client = _reducer_client(client)
    if hasattr(governor, "trace_recorder"):
        governor.trace_recorder.record("layered.reducer.started", {
            "group_id": group["group_id"],
            "timeout_seconds": getattr(reducer_client, "timeout", None),
            "max_retries": getattr(reducer_client, "max_retries", None),
        })
    response = reducer_client.chat([
        {"role": "system", "content": "你是通用 Local Reducer，只输出一个严格 JSON 对象，不输出 Markdown。不得预设领域、角色数量或章节。"},
        {"role": "user", "content": f"任务目标：{task_content}\n交付契约：{json.dumps(delivery_contract, ensure_ascii=False)}\n子图：{group['title']}\n证据包：{source}\n来源清单（由框架确定）：{json.dumps(canonical_refs, ensure_ascii=False)}\n只输出字段：group_id, source_nodes, verified_claims, conflicts, coverage_gaps, constraints, limitations, open_questions, recommended_followups, source_artifacts, task_version。source_artifacts 必须使用上面的来源清单；输入文件、其他生产者的产物与历史节点输出只是上下文，不是本组生产的证据。主张的 evidence_refs 必须是本组来源清单中的引用，不能拼接文件名或虚构节点。每个 verified_claims 项必须包含 claim、evidence_refs、confidence、epistemic_status，其中状态只能是 observed/derived/hypothesis/planned/unknown。coverage_gaps 必须是对象数组，每项包含 gap、severity、required_for_delivery、blocking_stage、evidence_needed、recommended_action；blocking_stage 只能是 evidence、synthesis 或 delivery。前置 Global Verifier 只阻断 evidence 阶段缺口；Final Synthesizer 尚未生成的文件、章节和格式属于 synthesis/delivery 阶段，不能在本阶段作为 evidence 缺口。只有缺失交付契约要求、明确交付章节或全局验收条件的内容才允许 required_for_delivery=true。尚未展开的方向、研究开放问题或可选增强必须放入 limitations/open_questions，不得冒充阻塞缺口。"},
    ])
    parsed = _parse_json(response)
    if not isinstance(parsed, dict) and hasattr(governor, "verify_evidence_summaries"):
        raise RuntimeError(f"Reducer {group['group_id']} returned non-JSON output")
    summary = _normalize_reducer(group, parsed, source_packets, task_version=task_version)
    if hasattr(governor, "trace_recorder"):
        governor.trace_recorder.record("layered.reducer.completed", {
            "group_id": group["group_id"],
            "claim_count": len(summary["verified_claims"]),
            "gap_count": len(summary["coverage_gaps"]),
        })
    return group["group_id"], summary


def build_layered_context(
    *,
    task_content: str,
    run_dir: Path,
    client: SynthesisChatClient,
    governor: Any,
    reducer_workers: int = 4,
) -> dict[str, Any]:
    """Run Governor grouping -> parallel JSON reducers -> Global Verifier -> outline."""
    task_version = int(getattr(governor, "goal_version", 1))
    graph = getattr(governor, "task_graph", None)
    packets = _load_packets(run_dir, task_version=task_version, graph=graph)
    if graph is not None:
        packets = [packet for packet in packets if (graph.get_node(packet["node_id"]) is not None and graph.get_node(packet["node_id"]).status == "completed")]
    if not packets:
        raise RuntimeError("No successful node outputs available for layered synthesis")
    layout = RunOutputLayout(run_dir)
    layered_dir = layout.layered_dir
    packet_dir = layout.evidence_dir
    reducer_dir = layout.reducer_dir
    packet_dir.mkdir(parents=True, exist_ok=True)
    reducer_dir.mkdir(parents=True, exist_ok=True)
    for packet in packets:
        _write_json(layout.packet_path(packet["packet_id"]), packet)

    delivery_contract = governor.derive_delivery_contract(task_content, client)
    decision = governor.plan_context_governance(task_content, packets, client)
    groups = decision.groups
    approved_ids = set(decision.retained_packet_ids) | set(decision.compressed_packet_ids)
    if not groups:
        raise RuntimeError("GlobalGovernor produced no reducer work groups")
    approved_packets = [packet for packet in packets if packet["node_id"] in approved_ids]
    by_id = {packet["node_id"]: packet for packet in approved_packets}
    decision.delivery_contract = delivery_contract
    _write_json(layered_dir / "governor_decision.json", {"groups": groups, "retained_packet_ids": decision.retained_packet_ids, "compressed_packet_ids": decision.compressed_packet_ids, "deferred_packet_ids": decision.deferred_packet_ids, "action_details": decision.action_details, "task_version": decision.task_version, "reason_codes": decision.reason_codes, "delivery_contract": delivery_contract, "reasoning": decision.reasoning})
    _write_json(layered_dir / "groups.json", groups)

    def reduce_group(group: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        source_packets = [by_id[node_id] for node_id in group["node_ids"] if node_id in by_id]
        return reduce_evidence_group(group, source_packets, task_content=task_content,
                                     delivery_contract=delivery_contract, client=client,
                                     task_version=task_version, governor=governor)

    reducers: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(reducer_workers, len(groups)))) as pool:
        futures = [pool.submit(reduce_group, group) for group in groups]
        for future in as_completed(futures):
            group_id, summary = future.result()
            reducers[group_id] = summary
    for group in groups:
        _write_json(reducer_dir / f"{group['group_id']}.json", reducers[group["group_id"]])
        # Human-readable mirror; the machine contract is the JSON artifact
        # above and all verification consumes that object.
        (reducer_dir / f"{group['group_id']}.md").write_text(
            json.dumps(reducers[group["group_id"]], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    reducer_refs = [f"layered/reducers/{group['group_id']}.json" for group in groups]
    if not hasattr(governor, "verify_evidence_summaries"):
        raise RuntimeError("GlobalGovernor does not expose the canonical Global Verifier")
    verifier = governor.verify_evidence_summaries(
        delivery_contract,
        reducers,
        artifact_refs=reducer_refs,
        task_version=task_version,
        run_dir=run_dir,
    )
    _write_json(layered_dir / "global_verifier.json", verifier)
    _write_json(layered_dir / "global_verification.json", verifier)
    if verifier.get("overall_status") == "fail":
        raise RuntimeError("Global Verifier rejected reducer evidence")

    synthesis_client = _synthesis_client(client)
    if hasattr(governor, "trace_recorder"):
        governor.trace_recorder.record("layered.outline.started", {
            "model": getattr(synthesis_client, "model", None),
            "timeout_seconds": getattr(synthesis_client, "timeout", None),
            "tools_enabled": False,
        })
    try:
        outline = synthesis_client.chat([
            {"role": "system", "content": "你是通用交付大纲编辑，只输出结构化 Markdown 大纲。不得重新规划任务。"},
            {"role": "user", "content": f"任务目标：{task_content}\n交付契约：{json.dumps(delivery_contract, ensure_ascii=False)}\n已验证主题摘要：{json.dumps(reducers, ensure_ascii=False)}\nGlobal Verifier 报告：{json.dumps(verifier, ensure_ascii=False)}\n生成适合该任务的最终交付大纲，只使用已验证主张并显式保留冲突、缺口和约束。"},
        ], max_tool_rounds=0).strip()
    except Exception as exc:
        if hasattr(governor, "trace_recorder"):
            governor.trace_recorder.record("layered.outline.failed", {
                "error_type": type(exc).__name__,
                "error": str(exc),
            })
        raise
    if hasattr(governor, "trace_recorder"):
        governor.trace_recorder.record("layered.outline.completed", {
            "chars": len(outline),
            "tools_enabled": False,
        })
    (layered_dir / "outline.md").write_text(outline, encoding="utf-8")
    return {"packets": packets, "approved_packets": approved_packets, "groups": groups, "reducers": reducers, "global_verifier": verifier, "global_verification": verifier, "outline": outline, "delivery_contract": delivery_contract, "deferred_packet_ids": decision.deferred_packet_ids, "context_actions": decision.action_details, "artifact_refs": ["layered/governor_decision.json", "layered/groups.json", "layered/global_verifier.json", "layered/global_verification.json", "layered/outline.md", *reducer_refs]}

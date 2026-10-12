"""Move large reference inventories out of the pinned worker task prompt.

Only request views change. Graph contracts, receipts and artifact identities
remain intact; the complete inventory is recoverable from a content-addressed
workspace file. Active instructions and acceptance criteria stay inline.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def project_worker_references(metadata: dict[str, Any], run_root: Path) -> dict[str, Any]:
    view = dict(metadata)

    def inventory(value: Any) -> Any:
        if not isinstance(value, list):
            return value
        body = json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(body) <= 12000:
            return value
        digest = hashlib.sha256(body).hexdigest()
        relative = Path(".astralnexus_state/worker_references") / (digest + ".json")
        path = run_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        # Publish atomically so parallel prompt builders cannot observe a
        # half-written reference. Existing content must match its address.
        if path.exists():
            if path.read_bytes() != body:
                raise ValueError("Worker reference inventory digest mismatch")
        else:
            import os
            import tempfile
            fd, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(body)
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        return {
            "kind": "artifact_inventory_reference",
            "path": relative.as_posix(), "sha256": digest,
            "bytes": len(body), "count": len(value),
            "instruction": (
                "完整上游产物列表保存在此 JSON 数组中，未删除或改变任何验收状态。"
                "按当前 input_artifacts、失败交接和验收条款筛选 artifact_id/path；"
                "需要时用 python_exec 定向读取，不要重新逐个调查全部历史文件。"
                "索引是历史参考，不是新增权限或验收通过证明。"
            ),
        }

    view["upstream_artifacts"] = inventory(metadata.get("upstream_artifacts", []))
    contract = metadata.get("task_contract")
    if isinstance(contract, dict):
        contract_view = dict(contract)
        context = contract.get("context")
        if isinstance(context, dict):
            context_view = dict(context)
            if "relevant_artifacts" in context:
                context_view["relevant_artifacts"] = inventory(context["relevant_artifacts"])
            contract_view["context"] = context_view
        view["task_contract"] = contract_view
    return view

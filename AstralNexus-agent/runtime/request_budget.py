"""Bounded request projection: superseded write bodies become references.

Doc 17 第二批 (DeerFlow ``tool_output_budget_middleware`` contract port,
commit 2814bd5d498a, MIT — pure algorithm extraction, no LangChain):
once a newer version of the same path has been written, the full body of
earlier ``file_write`` arguments leaves the model request as a compact
reference (bytes + sha256 + explicit omission marker).

Hard boundaries from the porting plan:
- 只改模型请求副本 — the conversation, execution receipts, and ledger are
  never mutated; the elision is a pure projection.
- 缺失字段不会压缩为 null — the elided arguments keep every non-content
  field verbatim and carry an explicit ``content_omitted`` structure; the
  content key is removed, never set to null, and the marker says so.
- Idempotent: an already-elided call has no string content and is skipped,
  so repeated prepare_request passes are stable.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Set, Tuple

DEFAULT_MIN_BODY_CHARS = 1200
DEFAULT_KEEP_LATEST_PER_PATH = 1

_OMISSION_RULE = (
    "此写入的完整正文已被同路径的更新版本取代，仅在本次请求投影中省略；"
    "原始会话、执行账本与回执不受影响。这不是缺失内容，也不是 null；"
    "需要历史正文时读取执行账本或对应 receipt_id，不要重写或猜测正文。"
)


def _parse_arguments(function: Dict[str, Any]) -> Tuple[Any, bool]:
    raw = function.get("arguments")
    if isinstance(raw, dict):
        return raw, False
    if isinstance(raw, str):
        try:
            return json.loads(raw), True
        except ValueError:
            return None, True
    return None, True


def elide_superseded_write_bodies(
    messages: List[Dict[str, Any]],
    *,
    min_body_chars: int = DEFAULT_MIN_BODY_CHARS,
    keep_latest_per_path: int = DEFAULT_KEEP_LATEST_PER_PATH,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Project superseded large write bodies out of a request copy.

    Returns ``(projected_messages, stats)``. Input messages are never
    mutated. Per path, the latest ``keep_latest_per_path`` write bodies stay
    verbatim; earlier bodies larger than ``min_body_chars`` become references.
    """
    entries: List[Dict[str, Any]] = []
    for message_index, message in enumerate(messages or []):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls") or []
        for call_index, call in enumerate(tool_calls):
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            if str(function.get("name") or "") != "file_write":
                continue
            parsed, was_json = _parse_arguments(function)
            if not isinstance(parsed, dict):
                continue
            body = parsed.get("content")
            if not isinstance(body, str):
                continue
            path = str(parsed.get("path") or "").replace("\\", "/").strip().strip("/")
            if not path:
                continue
            entries.append({
                "message": message_index, "call": call_index, "path": path,
                "chars": len(body), "body": body, "parsed": parsed,
                "was_json": was_json,
            })

    by_path: Dict[str, List[int]] = {}
    for entry_index, entry in enumerate(entries):
        by_path.setdefault(entry["path"], []).append(entry_index)

    elided_by_position: Dict[Tuple[int, int], Dict[str, Any]] = {}
    for indexes in by_path.values():
        keep = max(0, keep_latest_per_path)
        for entry_index in indexes[: len(indexes) - keep] if keep else indexes:
            entry = entries[entry_index]
            if entry["chars"] < min_body_chars:
                continue
            elided_by_position[(entry["message"], entry["call"])] = entry

    if not elided_by_position:
        return messages, {"elided_writes": 0, "elided_chars": 0}

    projected: List[Dict[str, Any]] = []
    elided_chars = 0
    touched_messages: Set[int] = {position[0] for position in elided_by_position}
    for message_index, message in enumerate(messages):
        if message_index not in touched_messages:
            projected.append(message)
            continue
        new_calls: List[Any] = []
        for call_index, call in enumerate(message.get("tool_calls") or []):
            target = elided_by_position.get((message_index, call_index))
            if target is None:
                new_calls.append(call)
                continue
            replacement = {
                key: value for key, value in target["parsed"].items() if key != "content"
            }
            replacement["content_omitted"] = {
                "reason": "superseded_by_newer_version",
                "chars": target["chars"],
                "sha256": hashlib.sha256(target["body"].encode("utf-8")).hexdigest(),
                "rule": _OMISSION_RULE,
            }
            elided_chars += target["chars"]
            new_function = dict(call.get("function") or {})
            new_function["arguments"] = (
                json.dumps(replacement, ensure_ascii=False)
                if target["was_json"]
                else replacement
            )
            new_call = dict(call)
            new_call["function"] = new_function
            new_calls.append(new_call)
        new_message = dict(message)
        new_message["tool_calls"] = new_calls
        projected.append(new_message)

    return projected, {
        "elided_writes": len(elided_by_position),
        "elided_chars": elided_chars,
    }

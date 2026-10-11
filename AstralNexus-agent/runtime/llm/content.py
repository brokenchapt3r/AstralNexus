"""Content-block structure helpers.

Port of ``@deepseek-ai/dsh-llm`` lib/types/content.js (commit 5badb150).
Messages and blocks are plain JSON mappings (possibly deep-frozen
projections); accessors use mapping reads.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..dshutil.values import assert_never

__all__ = [
    "content_has_file",
    "content_has_image",
    "file_handle_text",
    "offloaded_image_text",
    "project_files_to_text",
    "project_images_for_text_model",
    "project_offloaded_images",
    "project_tool_updates",
    "request_image_handle_text",
    "required_image_offload",
    "resolve_image_attachment_access",
    "text_only_image_text",
]


def resolve_image_attachment_access(
        attachments: Any, map_host_path: Callable[[str], Optional[str]],
        ref: Any) -> Optional[Dict[str, Any]]:
    """Bridge one attachment provider's host object location into the mounted
    tool execution world.

    :returns: a read-only execution-world path mapping, or None when either
        provider exposes no mapping.
    """
    host_path = attachments.imageHostPath(ref)
    if host_path is None:
        return None
    readonly_path = map_host_path(host_path)
    return None if readonly_path is None else {"readonlyPath": readonly_path}


def _quoted(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _image_identity(ref: Any) -> str:
    name = ref.get("name")
    if name is None:
        return str(ref["attachmentId"])
    return f"{_quoted(name)} ({ref['attachmentId']})"


def _extension(media_type: str) -> str:
    if media_type == "image/png":
        return ".png"
    if media_type == "image/jpeg":
        return ".jpg"
    if media_type == "image/webp":
        return ".webp"
    if media_type == "image/gif":
        return ".gif"
    return assert_never(media_type, "image extension")


def _normalized_access_text(ref: Any, access: Dict[str, Any]) -> str:
    return (
        " Normalized copy (read-only; may be resized or re-encoded): "
        f"{_quoted(access['readonlyPath'])} ({ref['width']}x{ref['height']}px,"
        f" {ref['mediaType']})."
        + " Source dimensions, format, and byte size may differ."
        + f" Copy to a writable path ending in {_extension(ref['mediaType'])}"
        " before editing.")


def text_only_image_text(ref: Any) -> str:
    """Stable text shown to a model that cannot accept one durable image
    reference."""
    digest = str(ref["attachmentId"])[len("sha256:"):len("sha256:") + 8]
    return ("[image omitted because this model accepts text only; "
            f"attachment sha256:{digest}]")


def request_image_handle_text(ref: Any, version: Dict[str, Any],
                              access: Optional[Dict[str, Any]]) -> str:
    """Stable model-facing handle for one exact request image."""
    preview = (f"Image {_image_identity(ref)}; request preview "
               f"{version['width']}x{version['height']}px.")
    if access is None:
        return (f"{preview} It may be resized or re-encoded; source "
                "dimensions, format, and byte size may differ.")
    return preview + _normalized_access_text(ref, access)


def offloaded_image_text(ref: Any,
                         access: Optional[Dict[str, Any]]) -> str:
    """Stable per-image placeholder for a request-limit omission."""
    identity = (f"image omitted to fit request image limits; "
                f"{_image_identity(ref)}.")
    if access is None:
        return (f"[{identity} No local normalized image path is available; "
                "ask the user to attach it again if needed.]")
    return f"[{identity}{_normalized_access_text(ref, access)}]"


def content_has_image(content: Sequence[Any]) -> bool:
    """True when typed model content contains an image block. This is the one
    image walk shared by every image policy, so a consumer cannot silently
    diverge."""
    return any(block["type"] == "image" for block in content)


def content_has_file(content: Sequence[Any]) -> bool:
    """True when typed model content contains a file block. Reads current
    content on every call without retaining scan results."""
    for block in content:
        if block["type"] == "file":
            return True
    return False


def file_handle_text(ref: Any, readonly_path: Optional[str]) -> str:
    """Stable model-facing handle for one durable file reference: the address
    of the verbatim stored copy and the instruction to read it on demand.
    This is the only representation a provider ever receives for a file."""
    digest = str(ref["attachmentId"])[len("sha256:"):len("sha256:") + 8]
    identity = (f"File {_quoted(ref['name'])} ({ref['bytes']} bytes, "
                f"sha256:{digest})")
    if readonly_path is None:
        return (f"[{identity} was uploaded, but the current execution "
                "environment cannot access a readable path. Report that "
                "limitation if its contents are needed; do not claim to "
                "have read it.]")
    return (f"[{identity}: verbatim read-only copy saved at "
            f"{_quoted(readonly_path)}. Read that path with your file tools "
            "when its contents are needed; copy it to a writable location "
            "before modifying it. When delegating file work, include this "
            "saved path in the delegation prompt; only subagents sharing "
            "this execution environment can read it.]")


def _replace_files_with_handles(
        blocks: Sequence[Any],
        resolve_path: Callable[[Any], Optional[str]]) -> Sequence[Any]:
    next_list: Optional[List[Any]] = None
    for index, block in enumerate(blocks):
        if block["type"] == "file":
            if next_list is None:
                next_list = list(blocks[:index])
            next_list.append({"type": "text",
                              "text": file_handle_text(
                                  block["attachment"],
                                  resolve_path(block["attachment"]))})
            continue
        if next_list is not None:
            next_list.append(block)
    return blocks if next_list is None else next_list


def project_files_to_text(
        messages: Sequence[Any],
        resolve_path: Callable[[Any], Optional[str]]) -> Sequence[Any]:
    """Replace every file occurrence with handle text."""
    if not any(content_has_file(message["content"]) for message in messages):
        return messages
    projected = []
    for message in messages:
        content = _replace_files_with_handles(message["content"], resolve_path)
        projected.append(message if content is message["content"]
                         else {**message, "content": content})
    return projected


def _base64_length(byte_count: int) -> int:
    """Base64 length of raw image bytes, including padding."""
    return math.ceil(byte_count / 3) * 4


def _visit_image_blocks(content: Sequence[Any],
                        visit: Callable[[Any], None]) -> None:
    for block in content:
        if block["type"] == "image":
            visit(block)


def _replace_offloaded_images(
        blocks: Sequence[Any],
        placeholder: Callable[[Any], str]) -> Sequence[Any]:
    next_list: Optional[List[Any]] = None
    for index, block in enumerate(blocks):
        if block["type"] == "image" and block.get("offloaded") is True:
            if next_list is None:
                next_list = list(blocks[:index])
            next_list.append({"type": "text",
                              "text": placeholder(block["attachment"])})
            continue
        if next_list is not None:
            next_list.append(block)
    return blocks if next_list is None else next_list


def project_offloaded_images(
        messages: Sequence[Any],
        placeholder: Callable[[Any], str]) -> List[Any]:
    """Replace every offloaded occurrence with its placeholder."""
    projected = []
    for message in messages:
        content = _replace_offloaded_images(message["content"], placeholder)
        projected.append(message if content is message["content"]
                         else {**message, "content": content})
    return projected


def _offloaded_image_prefix_count(lengths: Sequence[int],
                                  budget: Any) -> int:
    """Number of oldest retained image occurrences one route budget removes,
    in whole count and byte quanta, once the budget is exceeded."""
    total = sum(lengths)
    max_images = budget.get("maxImages")
    max_bytes = budget.get("maxBytes")
    excess_count = 0 if max_images is None \
        else max(0, len(lengths) - max_images)
    excess_bytes = 0 if max_bytes is None else max(0, total - max_bytes)
    if excess_count == 0 and excess_bytes == 0:
        return 0
    count_quantum = budget.get("countQuantum")
    if count_quantum is None:
        count_quantum = 1
    byte_quantum = budget.get("byteQuantum")
    if byte_quantum is None:
        byte_quantum = 1
    remove_count = 0 if excess_count == 0 \
        else math.ceil(excess_count / count_quantum) * count_quantum
    remove_bytes = 0 if excess_bytes == 0 \
        else math.ceil(excess_bytes / byte_quantum) * byte_quantum
    count = 0
    removed_bytes = 0
    for image_bytes in lengths:
        byte_target_met = remove_bytes == 0 or (
            removed_bytes >= remove_bytes if byte_quantum == 1
            else removed_bytes > remove_bytes)
        if count >= remove_count and byte_target_met:
            break
        removed_bytes += image_bytes
        count += 1
    return count


def required_image_offload(messages: Sequence[Any], budget: Any,
                           version_bytes: Callable[[Any], int]) -> int:
    """Number of oldest retained occurrences a route must still offload
    before a derived request fits its budget at the exact byte length the
    route sends; zero when the request fits."""
    lengths: List[int] = []
    for message in messages:
        def visit(block: Any) -> None:
            if block.get("offloaded") is True:
                return
            byte_count = version_bytes(block)
            lengths.append(_base64_length(byte_count)
                           if budget.get("representation") == "base64"
                           else byte_count)
        _visit_image_blocks(message["content"], visit)
    return _offloaded_image_prefix_count(lengths, budget)


def _replace_images_for_text_model(blocks: Sequence[Any]) -> Sequence[Any]:
    next_list: Optional[List[Any]] = None
    for index, block in enumerate(blocks):
        if block["type"] == "image":
            if next_list is None:
                next_list = list(blocks[:index])
            next_list.append({"type": "text",
                              "text": text_only_image_text(block["attachment"])})
            continue
        if next_list is not None:
            next_list.append(block)
    return blocks if next_list is None else next_list


def project_images_for_text_model(messages: Sequence[Any]) -> Sequence[Any]:
    """Replace every image occurrence for a text-only model."""
    if not any(content_has_image(message["content"]) for message in messages):
        return messages
    projected = []
    for message in messages:
        content = _replace_images_for_text_model(message["content"])
        projected.append(message if content is message["content"]
                         else {**message, "content": content})
    return projected


def _without_developer_messages(messages: Sequence[Any]) -> Sequence[Any]:
    retained = [message for message in messages
                if message["role"] != "developer"]
    return messages if len(retained) == len(messages) else retained


def _tool_declarations(tools: Optional[Sequence[Any]], mode: str,
                       history: Any) -> Dict[str, Dict[str, Any]]:
    declarations: Dict[str, Dict[str, Any]] = {
        tool["name"]: tool for tool in history["tools"]}
    for update in history["updates"]:
        for tool in update["additions"]:
            if tool["name"] not in declarations:
                # Later additions activate these definitions at their
                # recorded position.
                declarations[tool["name"]] = {**tool, "deferLoading": True}
    if mode == "in-history":
        # Removal blocks disable tools without discarding their historical
        # definitions.
        return declarations
    if mode == "addition-only":
        # Without removal support, the declaration list must omit inactive
        # tools.
        active_names = {tool["name"] for tool in (tools or [])}
        for name in list(declarations.keys()):
            if name not in active_names:
                del declarations[name]
        return declarations
    return assert_never(mode)


def project_tool_updates(messages: Sequence[Any],
                         tools: Optional[Sequence[Any]],
                         tool_update: Optional[str],
                         history: Optional[Any]) -> Dict[str, Any]:
    """Construct provider declarations from session-folded history without
    changing logged active tools. Unsupported routes and incomplete history
    use current declarations without developer updates. Explicitly deferred
    baseline tools become available only after their first retained
    addition."""
    if tool_update is None:
        # Unsupported routes need immediately available tools and no update
        # messages.
        immediate_tools = tools
        if tools is not None and any(
                tool.get("deferLoading") is True for tool in tools):
            immediate_tools = [{k: v for k, v in tool.items()
                                if k != "deferLoading"} for tool in tools]
        return {"messages": _without_developer_messages(messages),
                "tools": immediate_tools}
    if history is None:
        # Current schemas alone cannot resolve definitions referenced by past
        # updates.
        return {"messages": _without_developer_messages(messages),
                "tools": tools}
    message_ids = {message["id"] for message in messages
                   if message["role"] == "developer"}
    if any(update["messageId"] not in message_ids
           for update in history["updates"]):
        # An auxiliary prefix may omit updates needed to activate historical
        # declarations.
        return {"messages": _without_developer_messages(messages),
                "tools": tools}
    declarations = _tool_declarations(tools, tool_update, history)
    update_ids = {update["messageId"] for update in history["updates"]}
    # Deferred baseline tools still need their first addition to become
    # available.
    offered = {tool["name"] for tool in history["tools"]
               if not tool.get("deferLoading")}
    projected_messages: List[Any] = []
    for message in messages:
        if message["role"] != "developer":
            projected_messages.append(message)
            continue
        # Earlier declaration series do not govern the current tool set.
        if message["id"] not in update_ids:
            continue
        content = []
        for block in message["content"]:
            block_type = block["type"]
            if block_type == "tool-addition":
                # Only declared tools that are not already available need
                # activation.
                tool_name = block["toolName"]
                if tool_name not in declarations or tool_name in offered:
                    continue
                offered.add(tool_name)
                content.append(block)
            elif block_type == "tool-removal":
                # Addition-only routes cannot deactivate a tool through
                # history.
                if tool_update != "in-history":
                    continue
                if block["toolName"] in offered:
                    offered.discard(block["toolName"])
                    content.append(block)
            else:
                # Other core and plugin-defined blocks retain their content
                # and order.
                content.append(block)
        if len(content) == 0:
            continue
        if len(content) == len(message["content"]):
            projected_messages.append(message)
        else:
            projected_messages.append({**message, "content": content})
    unchanged = len(projected_messages) == len(messages) and all(
        projected_messages[index] is messages[index]
        for index in range(len(messages)))
    return {
        "messages": messages if unchanged else projected_messages,
        "tools": list(declarations.values()),
    }

"""第二批请求投影证明（doc 17）: 过去写入的完整正文在新版本出现后不重复进入
请求；只改模型请求副本，原始会话/账本不动；缺失字段不会压缩为 null；历史增多
不会令单次请求线性展开所有旧正文。"""
from __future__ import annotations

import json

from AstralNexus.runtime.request_budget import elide_superseded_write_bodies
from AstralNexus.runtime.request_sanitizer import prepare_request


def _write_call(path, body, mode="write", call_id="c1"):
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": "file_write",
            "arguments": json.dumps(
                {"path": path, "content": body, "mode": mode}, ensure_ascii=False
            ),
        },
    }


def _conversation():
    old_body = "OLD" * 800
    new_body = "NEW" * 800
    return [
        {"role": "user", "content": "implement"},
        {"role": "assistant", "content": "", "tool_calls": [_write_call("app/main.py", old_body, call_id="c1")]},
        {"role": "tool", "tool_call_id": "c1", "content": '{"status":"success"}'},
        {"role": "assistant", "content": "", "tool_calls": [_write_call("app/main.py", new_body, call_id="c2")]},
        {"role": "tool", "tool_call_id": "c2", "content": '{"status":"success"}'},
    ], old_body, new_body


def test_superseded_body_leaves_request_latest_stays():
    messages, old_body, new_body = _conversation()
    projected, stats = elide_superseded_write_bodies(messages)
    assert stats["elided_writes"] == 1
    assert stats["elided_chars"] == len(old_body)
    first_args = json.loads(projected[1]["tool_calls"][0]["function"]["arguments"])
    assert "content" not in first_args
    assert first_args["content_omitted"]["chars"] == len(old_body)
    assert first_args["content_omitted"]["reason"] == "superseded_by_newer_version"
    assert first_args["path"] == "app/main.py"
    assert first_args["mode"] == "write"
    latest_args = json.loads(projected[3]["tool_calls"][0]["function"]["arguments"])
    assert latest_args["content"] == new_body


def test_original_conversation_is_never_mutated():
    messages, _, _ = _conversation()
    before = json.dumps(messages, ensure_ascii=False, sort_keys=True)
    elide_superseded_write_bodies(messages)
    assert json.dumps(messages, ensure_ascii=False, sort_keys=True) == before


def test_missing_fields_never_become_null():
    messages, _, _ = _conversation()
    projected, _ = elide_superseded_write_bodies(messages)
    arguments = json.loads(projected[1]["tool_calls"][0]["function"]["arguments"])
    # the content key is removed with an explicit marker — never set to null,
    # never silently dropped without the omission structure
    assert "content" not in arguments
    assert set(arguments) == {"path", "mode", "content_omitted"}
    assert arguments["content_omitted"]["sha256"]


def test_small_bodies_and_single_versions_are_untouched():
    messages = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [_write_call("a.py", "small")]},
    ]
    projected, stats = elide_superseded_write_bodies(messages)
    assert stats["elided_writes"] == 0
    assert projected is messages


def test_idempotent_projection():
    messages, _, _ = _conversation()
    first, _ = elide_superseded_write_bodies(messages)
    second, stats2 = elide_superseded_write_bodies(first)
    assert stats2["elided_writes"] == 0
    assert json.dumps(second, ensure_ascii=False) == json.dumps(first, ensure_ascii=False)


def test_history_growth_does_not_expand_old_bodies():
    """20 rewrites of one path: the request carries only the latest body."""
    messages = [{"role": "user", "content": "iterate"}]
    for version in range(20):
        messages.append({
            "role": "assistant", "content": "",
            "tool_calls": [_write_call("app/main.py", f"v{version}" * 600, call_id=f"c{version}")],
        })
        messages.append({
            "role": "tool", "tool_call_id": f"c{version}", "content": '{"status":"success"}',
        })
    projected, stats = elide_superseded_write_bodies(messages)
    assert stats["elided_writes"] == 19
    bodies = [
        json.loads(call["function"]["arguments"]).get("content")
        for message in projected if message.get("role") == "assistant"
        for call in message.get("tool_calls") or []
    ]
    assert [body for body in bodies if body is not None] == ["v19" * 600]


def test_dict_arguments_are_elided_as_dict():
    body = "D" * 1500
    messages = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c1",
            "function": {"name": "file_write",
                         "arguments": {"path": "b.py", "content": body, "mode": "write"}},
        }]},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "c2",
            "function": {"name": "file_write",
                         "arguments": {"path": "b.py", "content": "final", "mode": "write"}},
        }]},
    ]
    projected, stats = elide_superseded_write_bodies(messages)
    assert stats["elided_writes"] == 1
    arguments = projected[1]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(arguments, dict)
    assert "content" not in arguments


def test_prepare_request_integration():
    messages, old_body, _ = _conversation()
    cleaned, _, stats = prepare_request(messages, None)
    assert stats["elided_writes"] == 1
    joined = json.dumps(cleaned, ensure_ascii=False)
    assert old_body not in joined
    assert "content_omitted" in joined

"""CLI consumer for the shared LLM stream event protocol."""
from __future__ import annotations

import sys
from typing import Any, Dict, Iterable


class CLIStreamRenderer:
    def __init__(self, *, output=None, diagnostics=None):
        self.output = output or sys.stdout
        self.diagnostics = diagnostics or sys.stderr
        self.text = []

    def __call__(self, event: Dict[str, Any]) -> None:
        event_type = event.get("event_type", "")
        if event_type == "llm.stream.delta":
            choices = event.get("event", {}).get("choices", [])
            delta = choices[0].get("delta", {}) if choices else {}
            content = delta.get("content") or ""
            if content:
                self.text.append(content)
                self.output.write(content)
                self.output.flush()
        elif event_type == "llm.stream.tool_call_delta":
            self.diagnostics.write("\n[tool call delta]\n")
            self.diagnostics.flush()
        elif event_type == "llm.stream.reconnected":
            self.diagnostics.write("\n[stream reconnected]\n")
            self.diagnostics.flush()
        elif event_type == "llm.stream.failed":
            self.diagnostics.write("\n[stream failed: %s]\n" % event.get("error", "unknown"))
            self.diagnostics.flush()

    @property
    def content(self) -> str:
        return "".join(self.text)


def stream_to_cli(client, messages, *, tools=None, max_tokens=None, temperature=None) -> str:
    renderer = CLIStreamRenderer()
    for _event in client.stream(
        messages,
        tools=tools,
        max_tokens=max_tokens,
        temperature=temperature,
        event_callback=renderer,
    ):
        pass
    return renderer.content

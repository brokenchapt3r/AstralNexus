from io import StringIO

from AstralNexus.runtime.stream_renderer import CLIStreamRenderer


def test_cli_stream_renderer_renders_text_and_tool_event():
    out = StringIO()
    diagnostics = StringIO()
    renderer = CLIStreamRenderer(output=out, diagnostics=diagnostics)
    renderer({
        "event_type": "llm.stream.delta",
        "event": {"choices": [{"delta": {"content": "hello"}}]},
    })
    renderer({"event_type": "llm.stream.tool_call_delta", "tool_calls": []})
    assert renderer.content == "hello"
    assert out.getvalue() == "hello"
    assert "tool call" in diagnostics.getvalue()

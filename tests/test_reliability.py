from AstralNexus.runtime.reliability import classify_http
from AstralNexus.tools.executor import ToolExecutor


def test_provider_error_categories_are_explicit():
    assert classify_http(401).category == "authentication"
    assert classify_http(429).retryable is True
    assert classify_http(400, "Range of input length").category == "context_overflow"


def test_idempotent_tool_failure_is_retried(monkeypatch):
    class Entry:
        def __init__(self):
            self.calls = 0

        def handler(self, _):
            self.calls += 1
            if self.calls == 1:
                raise TimeoutError("temporary")
            return '{"status":"success"}'

    entry = Entry()
    monkeypatch.setenv("ASTRALNEXUS_TOOL_RETRIES", "1")
    result = ToolExecutor()._dispatch_registry_tool("web_search", entry, {})
    assert result["status"] == "success"
    assert entry.calls == 2


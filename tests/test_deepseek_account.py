"""Contract tests for the shared account seam (dsh-deepseek-account port)."""
from __future__ import annotations

import pytest

from AstralNexus.runtime.account.base import (
    ACCOUNT_PROVIDER, DeepSeekAccount, cancel_signed_out_account_tasks,
    desktop_client_headers, is_account_sign_in_error, is_running_account_task,
    merge_platform_cookies, platform_client_headers, platform_wire_locale)


class TestMergePlatformCookies:
    def test_override_wins_and_unrelated_retained(self):
        merged = merge_platform_cookies("a=1; b=2", "b=3; c=4")
        assert merged == "a=1; b=3; c=4"  # first-seen order, override value

    def test_case_sensitive_names(self):
        assert merge_platform_cookies("Session=x", "session=y") == "Session=x; session=y"

    def test_malformed_pairs_skipped(self):
        assert merge_platform_cookies("novalue;=orphan;good=1", "") == "good=1"
        # Upstream quirk kept faithfully: a leading space shifts indexOf("=")
        # to 1, so the pair survives under the EMPTY name.
        assert merge_platform_cookies(" =orphan", "") == "=orphan"

    def test_values_trimmed_and_semicolons_in_value_kept(self):
        assert merge_platform_cookies("  a = 1 ;  ", "") == "a=1"
        # Only the FIRST "=" separates; a value may contain "=" but not ";"
        # (a ";" ends the pair, upstream's split boundary).
        assert merge_platform_cookies("tok=ab=cd", "") == "tok=ab=cd"

    def test_empty_headers(self):
        assert merge_platform_cookies("", "") == ""


class TestClientHeaders:
    def test_desktop_client_headers(self):
        assert desktop_client_headers(None) == {}
        assert desktop_client_headers("win32") == {"x-client-platform": "desktop-win"}
        assert desktop_client_headers("darwin") == {"x-client-platform": "desktop-mac"}

    def test_platform_wire_locale(self):
        assert platform_wire_locale("zh-CN") == "zh_CN"
        assert platform_wire_locale("zh_TW") == "zh_CN"
        assert platform_wire_locale("ZH") == "zh_CN"
        assert platform_wire_locale("zh") == "zh_CN"
        assert platform_wire_locale("en-US") == "en_US"
        assert platform_wire_locale("fr") == "en_US"

    def test_five_headers_web(self):
        client = {"version": "1.2.3", "locale": "zh-CN", "timezone_offset_seconds": -28800}
        headers = platform_client_headers(None, client)
        assert headers == {
            "x-client-bundle-id": "",  # intentionally empty
            "x-client-platform": "web",
            "x-client-version": "1.2.3",
            "x-client-locale": "zh_CN",
            "x-client-timezone-offset": "-28800",
        }

    def test_desktop_override_replaces_web(self):
        client = {"version": "1.0.0", "locale": "en-US", "timezone_offset_seconds": 0}
        headers = platform_client_headers("win32", client)
        assert headers["x-client-platform"] == "desktop-win"
        assert len(headers) == 5


class TestAccountServiceShell:
    def test_shell_keeps_upstream_service_key(self):
        assert DeepSeekAccount.service_name == "deepseekAccount"
        ctx = object()
        account = DeepSeekAccount(ctx)  # the shell itself is concrete-but-empty
        assert account.ctx is ctx
        assert issubclass(DeepSeekAccount, object)


class _LlmErrorLike(RuntimeError):
    def __init__(self, code):
        super().__init__(f"llm failure {code}")
        self.code = code


class TestAccountTaskDecisions:
    def test_sign_in_error_structural_discrimination(self):
        assert is_account_sign_in_error(_LlmErrorLike("ACCOUNT_SIGN_IN_REQUIRED"))
        assert not is_account_sign_in_error(_LlmErrorLike("RATE_LIMITED"))
        assert not is_account_sign_in_error(RuntimeError("no code"))
        assert not is_account_sign_in_error(None)
        assert not is_account_sign_in_error("ACCOUNT_SIGN_IN_REQUIRED")  # a string is not an error

    @staticmethod
    def _agent(status, provider=None, has_context=True, camel=False):
        class Session:
            def request_context(self):
                return {"provider": provider} if has_context else None

        class Agent:
            def __init__(self):
                self.status = status
                self.session = Session()
                self.cancellations = []
                if camel:  # the upstream camelCase accessor is honored too
                    self.session.requestContext = self.session.request_context

            def cancel(self, reason, options):
                self.cancellations.append((reason, options))

        return Agent()

    def test_running_account_task_discrimination(self):
        assert is_running_account_task(self._agent("running", ACCOUNT_PROVIDER))
        assert not is_running_account_task(self._agent("stopped", ACCOUNT_PROVIDER))
        assert not is_running_account_task(self._agent("running", "api-key"))
        assert not is_running_account_task(self._agent("running", None))
        assert not is_running_account_task(self._agent("running", has_context=False))
        assert is_running_account_task(self._agent("running", ACCOUNT_PROVIDER, camel=True))

    def test_sign_out_cancels_only_account_tasks(self):
        account_running = self._agent("running", ACCOUNT_PROVIDER)
        account_stopped = self._agent("stopped", ACCOUNT_PROVIDER)
        api_running = self._agent("running", "api-key")
        cancelled = cancel_signed_out_account_tasks([account_running, account_stopped, api_running])
        assert cancelled == 1
        assert account_running.cancellations == [
            ({"kind": "hook", "reason": "deepseek-account/signed-out"}, {"keepInbox": True})]
        assert account_stopped.cancellations == [] and api_running.cancellations == []

    def test_empty_roster(self):
        assert cancel_signed_out_account_tasks([]) == 0


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-q"])

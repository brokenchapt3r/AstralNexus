"""Contract tests for the native authorization seam and its host integration."""
from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from AstralNexus.runtime.authorization import (
    AuthorizationDeclinedError, AuthorizationError, AuthorizationRegistry, CancelToken)
from AstralNexus.runtime.credentials.local import LocalCredentialProvider
from AstralNexus.runtime.dshutil.launch_environment import (
    LaunchEnvironmentLayer, create_launch_environment_snapshot)

KEY = "demo/flow"
GRANT = {"kind": "grant", "payload": {"token": "t1"}}


@pytest.fixture()
def provider(tmp_path):
    made = []

    def build():
        instance = LocalCredentialProvider(
            {"path": str(tmp_path / "docs" / ".credentials.yaml"), "watch": False, "debounceMs": 10},
            launch_environment=create_launch_environment_snapshot(
                [LaunchEnvironmentLayer("process", {})]))
        instance.start()
        made.append(instance)
        return instance

    yield build
    for instance in made:
        instance.close()


@pytest.fixture()
def registry(provider):
    local = provider()
    return AuthorizationRegistry(credentials=lambda: local), local


class Surface:
    """A scripted interaction surface recording the neutral vocabulary."""

    def __init__(self, prompt_behavior=None):
        self.notices: List[Any] = []
        self.prompts: List[Any] = []
        self._prompt_behavior = prompt_behavior

    def interaction(self) -> Dict[str, Any]:
        return {"notify": self.notify, "prompt": self.prompt}

    def notify(self, notice):
        self.notices.append(notice)

    def prompt(self, question):
        self.prompts.append(question)
        if self._prompt_behavior is None:
            return "answered"
        return self._prompt_behavior(question)


def flow(run, key=KEY, label="Demo", methods=None):
    return {"key": key, "label": label,
            "methods": methods or [{"id": "oauth", "label": "Sign in"}], "run": run}


def begin_in_thread(registry, request, box):
    def worker():
        try:
            box["outcome"] = registry.begin(request)
        except BaseException as error:  # noqa: BLE001 - test evidence
            box["error"] = error

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread


class TestErrorTaxonomy:
    def test_codes(self):
        error = AuthorizationError("boom", "NO_FLOW")
        assert error.code == "NO_FLOW" and str(error) == "boom"
        declined = AuthorizationDeclinedError()
        assert declined.code == "DECLINED"
        assert str(declined) == "the authorization prompt was declined"
        assert isinstance(declined, AuthorizationError)


class TestCancelToken:
    def test_abort_idempotent_first_reason_wins(self):
        token = CancelToken()
        assert not token.aborted
        reason = RuntimeError("first")
        token.abort(reason)
        token.abort(RuntimeError("second"))
        assert token.aborted and token.reason is reason

    def test_throw_if_aborted(self):
        token = CancelToken()
        token.throw_if_aborted()  # quiet while live
        token.abort()
        with pytest.raises(AuthorizationError) as info:
            token.throw_if_aborted()
        assert info.value.code == "CANCELLED"
        custom = CancelToken()
        custom.abort(ValueError("specific"))
        with pytest.raises(ValueError, match="specific"):
            custom.throw_if_aborted()

    def test_listeners_fire_once_contained_and_unsubscribable(self):
        token = CancelToken()
        seen = []

        def broken():
            raise RuntimeError("listener exploded")

        unsubscribe = token.on_abort(lambda: seen.append("a"))
        token.on_abort(broken)
        token.on_abort(lambda: seen.append("c"))
        unsubscribe()
        unsubscribe()  # idempotent
        token.abort()  # the broken listener must not block the abort
        token.abort()
        assert token.aborted and seen == ["c"]  # exactly once


class TestRegistration:
    def test_flow_validation(self, registry):
        reg, _ = registry
        good = flow(lambda session: None)
        with pytest.raises(TypeError, match="must be a mapping"):
            reg.register_flow(object())
        with pytest.raises(TypeError, match="scope"):
            reg.register_flow(flow(lambda s: None, key="BAD"))
        with pytest.raises(TypeError, match="non-empty string"):
            reg.register_flow({**good, "label": ""})
        with pytest.raises(TypeError, match="at least one method"):
            reg.register_flow({**good, "methods": []})
        with pytest.raises(TypeError, match="non-empty string id"):
            reg.register_flow({**good, "methods": [{"id": ""}]})
        with pytest.raises(TypeError, match="unique"):
            reg.register_flow({**good, "methods": [{"id": "a"}, {"id": "a"}]})
        with pytest.raises(TypeError, match="callable"):
            reg.register_flow({**good, "run": None})

    def test_duplicate_flow_refused(self, registry):
        reg, _ = registry
        reg.register_flow(flow(lambda s: None))
        with pytest.raises(AuthorizationError) as info:
            reg.register_flow(flow(lambda s: None))
        assert info.value.code == "DUPLICATE_FLOW"

    def test_list_describe_and_disposer(self, registry):
        reg, _ = registry
        dispose_a = reg.register_flow(flow(lambda s: None, key="aaa/one"))
        reg.register_flow(flow(lambda s: None, key="bbb/two"))
        assert [item["key"] for item in reg.list()] == ["aaa/one", "bbb/two"]
        entry = reg.describe("aaa/one")
        assert entry == {"key": "aaa/one", "label": "Demo",
                         "methods": [{"id": "oauth", "label": "Sign in"}], "in_flight": False}
        assert reg.describe("zzz/none") is None
        dispose_a()
        dispose_a()  # idempotent
        assert [item["key"] for item in reg.list()] == ["bbb/two"]

    def test_disposer_cancels_running_attempt(self, registry):
        reg, _ = registry
        gate = threading.Event()
        entered = threading.Event()

        def run(session):
            entered.set()
            gate.wait(5)

        dispose = reg.register_flow(flow(run))
        box = {}
        thread = begin_in_thread(reg, {"key": KEY, "interaction": Surface().interaction()}, box)
        assert entered.wait(5)
        dispose()  # withdraws the flow AND cancels its attempt
        thread.join(5)
        assert box.get("outcome") == {"status": "cancelled"}
        gate.set()
        assert reg.list() == []


class TestBeginGuards:
    def test_no_flow_and_unknown_method(self, registry):
        reg, _ = registry
        reg.register_flow(flow(lambda s: None))
        surface = Surface().interaction()
        with pytest.raises(AuthorizationError) as info:
            reg.begin({"key": "other/key", "interaction": surface})
        assert info.value.code == "NO_FLOW"
        with pytest.raises(AuthorizationError) as info:
            reg.begin({"key": KEY, "method": "magic", "interaction": surface})
        assert info.value.code == "UNKNOWN_METHOD"
        with pytest.raises(AuthorizationError) as info:
            reg.begin({"key": KEY, "method": "", "interaction": surface})
        assert info.value.code == "UNKNOWN_METHOD"  # "" is not the default method

    def test_method_defaults_to_first(self, registry):
        reg, _ = registry
        seen = []

        def run(session):
            seen.append(session["method"])
            session["commit"](GRANT)

        reg.register_flow(flow(run, methods=[{"id": "first"}, {"id": "second"}]))
        assert reg.begin({"key": KEY, "interaction": Surface().interaction()}) == {
            "status": "authorized"}
        assert seen == ["first"]

    def test_preaborted_signal_cancels_without_running(self, registry):
        reg, _ = registry
        ran = []
        reg.register_flow(flow(lambda s: ran.append(1)))
        signal = CancelToken()
        signal.abort()
        assert reg.begin({"key": KEY, "signal": signal,
                          "interaction": Surface().interaction()}) == {"status": "cancelled"}
        assert ran == []

    def test_already_in_flight_refused_not_joined(self, registry):
        reg, _ = registry
        gate = threading.Event()
        entered = threading.Event()

        def run(session):
            entered.set()
            gate.wait(5)
            session["commit"](GRANT)

        reg.register_flow(flow(run))
        box = {}
        thread = begin_in_thread(reg, {"key": KEY, "interaction": Surface().interaction()}, box)
        assert entered.wait(5)
        with pytest.raises(AuthorizationError) as info:
            reg.begin({"key": KEY, "interaction": Surface().interaction()})
        assert info.value.code == "ALREADY_IN_FLIGHT"
        assert reg.describe(KEY)["in_flight"] is True
        gate.set()
        thread.join(5)
        assert box["outcome"] == {"status": "authorized"}
        assert reg.describe(KEY)["in_flight"] is False  # released after settle

    def test_missing_credentials_fails_loud(self, provider):
        reg = AuthorizationRegistry(credentials=lambda: None)
        reg.register_flow(flow(lambda s: None))
        with pytest.raises(AuthorizationError) as info:
            reg.begin({"key": KEY, "interaction": Surface().interaction()})
        assert info.value.code == "NO_CREDENTIALS"

    def test_interaction_surface_required(self, registry):
        reg, _ = registry
        reg.register_flow(flow(lambda s: None))
        for bad in (None, {}, {"notify": lambda n: None}, {"prompt": lambda p: None}):
            with pytest.raises(TypeError, match="notify and prompt"):
                reg.begin({"key": KEY, "interaction": bad})


class TestAttemptOutcomes:
    def test_authorized_commits_through_the_seam(self, registry):
        reg, local = registry
        surface = Surface()
        events = []
        reg.on("authorization/settled", lambda key, settlement: events.append((key, settlement)))

        def run(session):
            session["notify"]({"message": "Continue in your browser"})
            answer = session["prompt"]({"question": "Paste the code"})
            assert answer == "answered"
            session["commit"]({"kind": "grant", "payload": {"token": "t1", "code": answer}})

        reg.register_flow(flow(run))
        assert reg.begin({"key": KEY, "interaction": surface.interaction()}) == {
            "status": "authorized"}
        assert surface.notices == [{"message": "Continue in your browser"}]
        assert local.read_record(KEY)["payload"]["code"] == "answered"  # durable
        assert events == [(KEY, "authorized")]

    def test_resolved_without_committing_is_not_committed(self, registry):
        reg, _ = registry
        events = []
        reg.on("authorization/settled", lambda key, settlement: events.append(settlement))
        reg.register_flow(flow(lambda session: session["notify"]({"message": "gave up"})))
        with pytest.raises(AuthorizationError) as info:
            reg.begin({"key": KEY, "interaction": Surface().interaction()})
        assert info.value.code == "NOT_COMMITTED"
        assert "resolved without committing" in str(info.value)
        assert events == ["failed"]

    def test_deleted_record_instead_of_committing(self, registry):
        reg, local = registry
        local.modify_record(KEY, lambda current: GRANT)

        def run(session):
            session["commit"](GRANT)
            local.delete_record(KEY)  # sabotages the commit contract

        reg.register_flow(flow(run))
        with pytest.raises(AuthorizationError) as info:
            reg.begin({"key": KEY, "interaction": Surface().interaction()})
        assert info.value.code == "NOT_COMMITTED"
        assert "deleted its credential record" in str(info.value)

    def test_broken_notice_surface_never_fails_the_flow(self, registry):
        reg, _ = registry

        def explode(notice):
            raise RuntimeError("surface is broken")

        def run(session):
            session["notify"]({"message": "ignored"})
            session["commit"](GRANT)

        reg.register_flow(flow(run))
        outcome = reg.begin({"key": KEY, "interaction": {
            "notify": explode, "prompt": lambda question: "ok"}})
        assert outcome == {"status": "authorized"}

    def test_declined_prompt_settles_cancelled(self, registry):
        reg, local = registry
        events = []
        reg.on("authorization/settled", lambda key, settlement: events.append(settlement))

        def decline(question):
            raise AuthorizationDeclinedError()

        def run(session):
            session["prompt"]({"question": "Allow?"})  # raises DECLINED
            session["commit"](GRANT)  # never reached

        reg.register_flow(flow(run))
        assert reg.begin({"key": KEY, "interaction": {
            "notify": lambda notice: None, "prompt": decline}}) == {"status": "cancelled"}
        assert local.read_record(KEY) is None
        assert events == ["cancelled"]

    def test_genuine_failure_rethrows_and_settles_failed(self, registry):
        reg, _ = registry
        events = []
        reg.on("authorization/settled", lambda key, settlement: events.append(settlement))

        def run(session):
            raise RuntimeError("exchange exploded")

        reg.register_flow(flow(run))
        with pytest.raises(RuntimeError, match="exchange exploded"):
            reg.begin({"key": KEY, "interaction": Surface().interaction()})
        assert events == ["failed"]

    def test_failure_after_abort_reads_as_cancelled(self, registry):
        reg, _ = registry
        gate = threading.Event()
        aborted = threading.Event()

        def run(session):
            gate.wait(5)
            raise RuntimeError("late failure")

        reg.register_flow(flow(run))
        signal = CancelToken()
        box = {}
        thread = begin_in_thread(reg, {"key": KEY, "signal": signal,
                                       "interaction": Surface().interaction()}, box)
        time.sleep(0.1)

        def abort_once_started():
            signal.abort()
            aborted.set()

        abort_once_started()
        gate.set()
        thread.join(5)
        assert aborted.is_set()
        assert box["outcome"] == {"status": "cancelled"}  # not the late RuntimeError


class TestCancellation:
    def test_cancel_key_withdraws_running_attempt(self, registry):
        reg, _ = registry
        gate = threading.Event()
        entered = threading.Event()
        settled = []
        reg.on("authorization/settled", lambda key, settlement: settled.append(settlement))

        def run(session):
            entered.set()
            gate.wait(10)  # a flow that never cooperates with cancellation

        reg.register_flow(flow(run))
        box = {}
        thread = begin_in_thread(reg, {"key": KEY, "interaction": Surface().interaction()}, box)
        assert entered.wait(5)
        assert reg.cancel(KEY) is True
        thread.join(5)  # the caller is released WITHOUT waiting for the flow
        assert box["outcome"] == {"status": "cancelled"}
        assert settled == ["cancelled"]
        gate.set()  # the abandoned flow finishes in the background (reaped)
        thread.join()

    def test_withdrawn_flow_after_fact_failure_is_contained(self, registry, caplog):
        reg, _ = registry
        entered = threading.Event()

        def run(session):
            entered.set()
            time.sleep(0.3)
            raise RuntimeError("after-the-fact failure")

        reg.register_flow(flow(run))
        box = {}
        thread = begin_in_thread(reg, {"key": KEY, "interaction": Surface().interaction()}, box)
        assert entered.wait(5)
        reg.cancel(KEY)
        thread.join(5)
        assert box["outcome"] == {"status": "cancelled"}
        time.sleep(0.6)  # let the reaper run; a raise here would fail the test loudly

    def test_request_signal_withdraws_through_the_chain(self, registry):
        reg, _ = registry
        entered = threading.Event()

        def run(session):
            entered.set()
            session["signal"].wait(10)

        reg.register_flow(flow(run))
        signal = CancelToken()
        box = {}
        thread = begin_in_thread(reg, {"key": KEY, "signal": signal,
                                       "interaction": Surface().interaction()}, box)
        assert entered.wait(5)
        signal.abort()
        thread.join(5)
        assert box["outcome"] == {"status": "cancelled"}

    def test_commit_after_cancel_is_refused(self, registry):
        reg, _ = registry
        entered = threading.Event()
        observed = {}

        def run(session):
            entered.set()
            session["signal"].wait(10)  # returns as soon as the cancel aborts
            try:
                session["commit"](GRANT)
            except AuthorizationError as error:
                observed["code"] = error.code

        reg.register_flow(flow(run))
        box = {}
        thread = begin_in_thread(reg, {"key": KEY, "interaction": Surface().interaction()}, box)
        assert entered.wait(5)
        reg.cancel(KEY)
        thread.join(5)
        assert box["outcome"] == {"status": "cancelled"}
        assert observed.get("code") == "CANCELLED"  # the guard, not a torn write

    def test_committing_attempt_is_never_interrupted(self, registry):
        reg, local = registry
        committed = threading.Event()
        release = threading.Event()

        def run(session):
            session["commit"](GRANT)  # committing flips True inside
            committed.set()
            release.wait(10)  # a cancel during this window must NOT abort

        reg.register_flow(flow(run))
        box = {}
        thread = begin_in_thread(reg, {"key": KEY, "interaction": Surface().interaction()}, box)
        assert committed.wait(5)
        assert reg.cancel(KEY) is False  # already committing: refused
        release.set()
        thread.join(5)
        assert box["outcome"] == {"status": "authorized"}
        assert local.read_record(KEY) is not None  # the commit survived


class TestSettledFanOut:
    def test_contained_listeners_and_invariant_rethrow(self, registry):
        reg, _ = registry

        class Invariant(RuntimeError):
            code = "INVARIANT"

        seen = []

        def broken(key, settlement):
            raise RuntimeError("watcher exploded")

        def invariant(key, settlement):
            raise Invariant("structure violated")

        reg.register_flow(flow(lambda session: session["commit"](GRANT)))
        reg.on("authorization/settled", broken)
        unwatch_invariant = reg.on("authorization/settled", invariant)
        unwatch = reg.on("authorization/settled", lambda key, settlement: seen.append(settlement))
        with pytest.raises(Invariant):
            reg.begin({"key": KEY, "interaction": Surface().interaction()})
        assert seen == ["authorized"]  # every listener ran before the rethrow
        unwatch()
        unwatch_invariant()
        # With the invariant listener gone the same attempt settles cleanly
        # (the broken listener stays: contained failures never surface).
        assert reg.begin({"key": KEY, "interaction": Surface().interaction()}) == {
            "status": "authorized"}
        assert seen == ["authorized"]

    def test_awaitable_listener_closed_not_awaited(self, registry):
        reg, _ = registry

        async def async_listener(key, settlement):
            pass

        reg.register_flow(flow(lambda session: session["commit"](GRANT)))
        reg.on("authorization/settled", async_listener)
        assert reg.begin({"key": KEY, "interaction": Surface().interaction()}) == {
            "status": "authorized"}  # no hang, no raise


class TestHostIntegration:
    def test_authorization_projection_and_cancel_route(self, tmp_path, monkeypatch):
        from AstralNexus.dashboard.workbench_server import WorkbenchHost

        monkeypatch.setenv("DSH_HOME", str(tmp_path / "dsh-home"))
        instance = WorkbenchHost(tmp_path / "wb")
        try:
            client = instance.app.test_client()
            body = client.get("/api/host/authorization").get_json()
            assert body == {"credentials_available": False, "flows": []}
            snapshot = client.get("/api/host").get_json()
            assert snapshot["authorization"] == body
            assert instance.plugins.services["authorization"] is instance.authorization

            document = tmp_path / "docs" / ".credentials.yaml"
            instance.plugins.put_config("credentials-local",
                                        {"path": str(document), "watch": False})
            instance.plugins.set_enabled("credentials-local", True)
            dispose = instance.authorization.register_flow({
                "key": KEY, "label": "Demo", "methods": [{"id": "oauth"}],
                "run": lambda session: None})
            body = client.get("/api/host/authorization").get_json()
            assert body["credentials_available"] is True
            assert body["flows"] == [{"key": KEY, "label": "Demo",
                                      "methods": [{"id": "oauth"}], "in_flight": False}]
            response = client.post(f"/api/host/authorization/{KEY}/cancel")
            assert response.status_code == 200
            assert response.get_json() == {"key": KEY, "cancelled": False}  # nothing running
            dispose()
            assert client.get("/api/host/authorization").get_json()["flows"] == []
        finally:
            instance.close()

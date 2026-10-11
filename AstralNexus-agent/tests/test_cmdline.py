"""Contract tests for AstralNexus.runtime.cmdline (dsh-cmdline port).

Covers the immutable snapshot handoff, the stdio EOF lifecycle (ready-gated
exit, latch, disposal, already-ended streams), the argparse adaptation of
parseCmdline (handler execution, terminal SystemExit routing through
appExit, output stream routing, the missing-action guard), and the
structural has_action tree walk.
"""
from __future__ import annotations

import argparse
import io
import sys
import threading
import time
from typing import Any, Dict, List

import pytest

from AstralNexus.runtime import cmdline


class FakeReady:
    """The launcher's successful-startup signal (AppReady analogue)."""

    def __init__(self) -> None:
        self.callbacks: List[Any] = []
        self.committed = False
        self.cancelled = 0

    def on_ready(self, callback):
        self.callbacks.append(callback)
        if self.committed:
            callback()
        return lambda: self._cancel(callback)

    def _cancel(self, callback) -> None:
        self.cancelled += 1
        if callback in self.callbacks:
            self.callbacks.remove(callback)

    def commit(self) -> None:
        self.committed = True
        for callback in list(self.callbacks):
            callback()


def wait_for(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@pytest.fixture()
def stdin_swap(monkeypatch):
    def swap(stream):
        monkeypatch.setitem(cmdline.internals, "stdin", stream)
    return swap


class TestSnapshotHandoff:
    def test_snapshot_is_frozen_tuple(self) -> None:
        args = ["--flag", "value"]
        services: Dict[str, Any] = {}
        cmdline.provide_cmdline(services, {"args": args, "exit": lambda c: None})
        snapshot = services["cmdlineArgs"]
        assert snapshot.get() == ("--flag", "value")
        args.append("--mutated")  # the caller's list cannot reach the tree
        assert snapshot.get() == ("--flag", "value")
        assert snapshot.get() is snapshot.get()  # same immutable tuple

    def test_exit_and_optional_ready(self) -> None:
        services: Dict[str, Any] = {}
        exit_calls: List[int] = []
        exit_fn = exit_calls.append  # a stable reference (bound builtin
        # methods are transient per attribute access)
        cmdline.provide_cmdline(services, {"args": [], "exit": exit_fn})
        assert services["appExit"] is exit_fn
        assert "appReady" not in services  # optional signal stays absent
        ready = FakeReady()
        cmdline.provide_cmdline(services, {"args": [], "exit": lambda c: None,
                                          "ready": ready})
        assert services["appReady"] is ready

    def test_empty_args_for_embedding_hosts(self) -> None:
        services: Dict[str, Any] = {}
        cmdline.provide_cmdline(services, {"args": [], "exit": lambda c: None})
        assert services["cmdlineArgs"].get() == ()


class TestExitOnStdinEnd:
    def test_missing_services_fail_loud(self, stdin_swap) -> None:
        stdin_swap(io.StringIO(""))
        with pytest.raises(RuntimeError,
                           match="the launcher must provide ctx.appExit and "
                                 "ctx.appReady"):
            cmdline.exit_on_stdin_end({}, "app")

    def test_eof_exits_zero_after_ready_commit(self, stdin_swap) -> None:
        stdin_swap(io.StringIO(""))  # already-ended stream
        ready = FakeReady()
        exits: List[int] = []
        services = {"appExit": exits.append, "appReady": ready}
        dispose = cmdline.exit_on_stdin_end(services, "app")
        # EOF observed, but the exit waits for the startup to commit — a
        # startup rejection racing EOF remains the process outcome
        assert wait_for(lambda: ready.callbacks)
        assert exits == []
        ready.commit()
        assert exits == [0]
        dispose()

    def test_dispose_latches_off_the_watcher(self, stdin_swap) -> None:
        stdin_swap(io.StringIO(""))
        ready = FakeReady()
        exits: List[int] = []
        services = {"appExit": exits.append, "appReady": ready}
        dispose = cmdline.exit_on_stdin_end(services, "app")
        dispose()  # before the watcher reaches EOF
        ready.committed = True
        time.sleep(0.2)
        ready.commit()
        assert exits == []
        assert ready.cancelled >= 1

    def test_ready_already_committed_exits_on_eof(self, stdin_swap) -> None:
        stdin_swap(io.StringIO(""))
        ready = FakeReady()
        ready.committed = True
        exits: List[int] = []
        services = {"appExit": exits.append, "appReady": ready}
        cmdline.exit_on_stdin_end(services, "app")
        assert wait_for(lambda: exits == [0])

    def test_stream_data_then_eof(self, stdin_swap) -> None:
        stdin_swap(io.StringIO("protocol bytes\nmore\n"))
        ready = FakeReady()
        exits: List[int] = []
        services = {"appExit": exits.append, "appReady": ready}
        cmdline.exit_on_stdin_end(services, "app")
        assert wait_for(lambda: bool(ready.callbacks))
        ready.commit()
        assert exits == [0]

    def test_closed_stream_counts_as_end(self, stdin_swap) -> None:
        stream = io.StringIO("")
        stream.close()
        stdin_swap(stream)
        ready = FakeReady()
        exits: List[int] = []
        services = {"appExit": exits.append, "appReady": ready}
        cmdline.exit_on_stdin_end(services, "app")
        assert wait_for(lambda: bool(ready.callbacks))
        ready.commit()
        assert exits == [0]


def program_with(**kwargs) -> argparse.ArgumentParser:
    program = argparse.ArgumentParser(prog="demo", **kwargs)
    program.add_argument("--flag")
    program.set_defaults(cmdline_handler=lambda ns: f"handled {ns.flag}")
    return program


class TestParseCmdline:
    def services(self, args, exits=None):
        return {"cmdlineArgs": cmdline.CmdlineArgs(args),
                "appExit": (exits if exits is not None else []).append}

    def test_handler_runs_on_success(self) -> None:
        exits: List[int] = []
        services = self.services(["--flag", "v"], exits)
        assert cmdline.parse_cmdline(services, program_with()) == "handled v"
        assert exits == []

    def test_missing_services_fail_loud(self) -> None:
        with pytest.raises(RuntimeError,
                           match="demo: the launcher must provide "
                                 "ctx.cmdlineArgs and ctx.appExit"):
            cmdline.parse_cmdline({}, program_with())

    def test_missing_action_fails_loud(self) -> None:
        empty = argparse.ArgumentParser(prog="empty")
        with pytest.raises(RuntimeError,
                           match="empty: no command in the program declares "
                                 "an action"):
            cmdline.parse_cmdline(self.services([]), empty)

    def test_help_is_terminal_through_app_exit(self, monkeypatch) -> None:
        exits: List[int] = []
        out = io.StringIO()
        monkeypatch.setitem(cmdline.internals, "stdout", out)
        services = self.services(["--help"], exits)
        assert cmdline.parse_cmdline(services, program_with()) is None
        assert exits == [0]
        assert "--flag" in out.getvalue()  # help text via the routed stream

    def test_grammar_rejection_is_terminal(self, monkeypatch) -> None:
        exits: List[int] = []
        err = io.StringIO()
        monkeypatch.setitem(cmdline.internals, "stderr", err)
        services = self.services(["--nope"], exits)
        assert cmdline.parse_cmdline(services, program_with()) is None
        assert exits == [2]  # argparse's usage-error code
        assert "usage" in err.getvalue()

    def test_handler_error_routes_through_app_exit(self) -> None:
        exits: List[int] = []

        def rejecting(ns):
            raise SystemExit(3)  # program.error(...) analogue

        program = program_with()
        program.set_defaults(cmdline_handler=rejecting)
        services = self.services([], exits)
        assert cmdline.parse_cmdline(services, program) is None
        assert exits == [3]

    def test_streams_are_restored_after_parse(self) -> None:
        before_out, before_err = sys.stdout, sys.stderr
        cmdline.parse_cmdline(self.services([]), program_with())
        assert sys.stdout is before_out
        assert sys.stderr is before_err

    def test_explicit_handler_wins(self) -> None:
        services = self.services(["--flag", "x"])
        result = cmdline.parse_cmdline(
            services, program_with(), handler=lambda ns: f"explicit {ns.flag}")
        assert result == "explicit x"

    def test_subcommand_handler_resolves_from_namespace(self) -> None:
        program = argparse.ArgumentParser(prog="multi")
        subs = program.add_subparsers(dest="command")
        sub = subs.add_parser("run")
        sub.set_defaults(cmdline_handler=lambda ns: "ran")
        subs.add_parser("other")  # a declared-but-handlerless sibling
        services = self.services(["run"])
        assert cmdline.parse_cmdline(services, program) == "ran"

    def test_has_action_tree_walk(self) -> None:
        assert cmdline.has_action(program_with()) is True
        empty = argparse.ArgumentParser(prog="empty")
        assert cmdline.has_action(empty) is False
        program = argparse.ArgumentParser(prog="multi")
        subs = program.add_subparsers()
        subs.add_parser("leaf").set_defaults(cmdline_handler=lambda ns: None)
        assert cmdline.has_action(program) is True  # found in the subtree

    def test_non_terminal_errors_rethrow(self) -> None:
        def exploding(ns):
            raise ValueError("not a control-flow error")

        program = program_with()
        program.set_defaults(cmdline_handler=exploding)
        with pytest.raises(ValueError, match="not a control-flow error"):
            cmdline.parse_cmdline(self.services([]), program)

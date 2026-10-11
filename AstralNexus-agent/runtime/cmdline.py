"""Native port of @deepseek-ai/dsh-cmdline (commit 5badb150, lib/index.js
152 lines read in full) — the command line a dsh launcher hands to the app
it boots.

The launcher parses only its own flags (``--home``, ``--port``, the config
dumps) and hands everything after them to the tree verbatim through the
``cmdlineArgs`` service, so an app owns its flag family, its ``--help``
text, and its parse errors instead of the launcher knowing them.

Any app plugin can publish ``cmdlineArgs`` consumers and call
``parse_cmdline``. A provider may publish the parsed values as its own
service from its program's action, and ordinary rows can read that service
from lazily resolved config — a flag beats the value written beside it.

Python adaptation notes (documented deviations, contract preserved):
- commander ``program`` → ``argparse.ArgumentParser``; the action-handler
  precondition (upstream ``hasAction``'s structural ``_actionHandler``
  walk) becomes a structural ``cmdline_handler`` default walk over the
  parser tree — a program that forgot its handler still fails loud here
  instead of publishing nothing and stranding dependents.
- commander's control-flow errors (help/version/parse rejection/
  ``program.error``) → argparse's ``SystemExit``; both are terminal for
  the process and route through ``appExit`` instead of the interpreter.
- ``isCommanderError``'s structural detection (out-of-tree plugins carry
  their own commander copy) has no Python analogue needed: ``SystemExit``
  is a language-level terminal type.
- ``configureExitAndOutput``'s recursive output routing → argparse reads
  ``sys.stdout``/``sys.stderr`` dynamically at print time, so the streams
  are swapped around the parse and restored afterwards; every command in
  the tree (subparsers included) is covered by the same swap.
- ``exitOnStdinEnd``'s event-listener + ``queueMicrotask`` pair → a daemon
  watch thread whose first read of an already-ended stream returns
  immediately (same "already ended fires on the next turn" behaviour);
  disposal flips the latch and cancels the readiness subscription — a
  Python thread blocked in ``readline`` cannot be unregistered, so the
  latch, not listener removal, is the guarantee (documented).
"""
from __future__ import annotations

import argparse
import sys
import threading
from typing import Any, Callable, Dict, Optional, Tuple

# Upstream service names, verbatim (Cordis camelCase contract vocabulary —
# consumers such as dsh-hmr read ctx.get("appReady") by these names).
CMDLINE_ARGS_KEY = "cmdlineArgs"
APP_EXIT_KEY = "appExit"
APP_READY_KEY = "appReady"


class CmdlineArgs:
    """The launcher's immutable argument snapshot.

    Frozen at provide time; ``get()`` returns the same tuple every call, so
    a later ``sys.argv`` mutation cannot reach an app's parse.
    """

    __slots__ = ("_snapshot",)

    def __init__(self, args: Any) -> None:
        self._snapshot: Tuple[str, ...] = tuple(args)

    def get(self) -> Tuple[str, ...]:
        return self._snapshot


#: Process streams used by app command lines and stdio lifetime binding;
#: tests substitute them (upstream ``internals`` seam, verbatim role).
internals: Dict[str, Any] = {
    "stdin": sys.stdin,
    "stdout": sys.stdout,
    "stderr": sys.stderr,
}


def provide_cmdline(services: Dict[str, Any], host: Dict[str, Any]) -> None:
    """Provide launcher facts before any tree entry mounts.

    ``host`` carries ``args`` (the invocation's arguments; an embedding host
    with no command line provides an empty list), ``exit`` (the bounded exit
    request), and optionally ``ready`` (the successful-startup signal a host
    mounting a stdio application provides).
    """
    snapshot = CmdlineArgs(host["args"])
    services[CMDLINE_ARGS_KEY] = snapshot
    services[APP_EXIT_KEY] = host["exit"]
    ready = host.get("ready")
    if ready is not None:
        services[APP_READY_KEY] = ready


def exit_on_stdin_end(services: Dict[str, Any], label: str) -> Callable[[], None]:
    """Make stdin EOF request the launcher's bounded shutdown after ready.

    A startup rejection therefore remains the process outcome when it races
    EOF. The caller invokes this only after its command action accepts the
    invocation, so help and usage failures start no transport lifecycle.
    The watcher does not consume stdin bytes for the protocol transport: it
    reads only to observe EOF. The returned disposer removes the readiness
    subscription and latches the watcher off.

    :param services: app plugin services carrying ``appExit``/``appReady``.
    :param label: effect label naming the owning application.
    :raises RuntimeError: when the launcher did not provide both services.
    """
    exit_request = services.get(APP_EXIT_KEY)
    ready = services.get(APP_READY_KEY)
    if exit_request is None or ready is None:
        raise RuntimeError(
            "stdio app: the launcher must provide ctx.appExit and ctx.appReady "
            "before the tree mounts")
    stdin = internals["stdin"]
    state: Dict[str, Any] = {"active": True, "ended": False,
                             "cancel_ready": lambda: None}

    def on_end() -> None:
        if not state["active"] or state["ended"]:
            return
        state["ended"] = True
        state["cancel_ready"] = ready.on_ready(lambda: exit_request(0))

    def watch() -> None:
        try:
            while True:
                chunk = stdin.readline()
                if not chunk:
                    break  # EOF (an already-ended stream returns at once)
        except (ValueError, OSError):
            pass  # the stream was closed underneath the watcher
        on_end()

    worker = threading.Thread(target=watch, name=f"cmdline-stdin-{label}",
                              daemon=True)
    worker.start()

    def dispose() -> None:
        state["active"] = False
        state["cancel_ready"]()

    return dispose


def has_action(program: argparse.ArgumentParser) -> bool:
    """Whether any command in the parser tree declares a handler.

    The argparse type cannot express the action precondition, so the
    handler is read structurally (as upstream reads commander's
    ``_actionHandler``): without this guard, a program that forgot its
    handler would parse successfully, publish nothing, and surface only as
    dependent rows pending on the absent service.
    """
    if callable(program.get_default("cmdline_handler")):
        return True
    for action in program._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            for sub in choices.values():
                if isinstance(sub, argparse.ArgumentParser) and has_action(sub):
                    return True
    return False


def parse_cmdline(services: Dict[str, Any],
                  program: argparse.ArgumentParser,
                  handler: Optional[Callable[[argparse.Namespace], Any]] = None
                  ) -> Any:
    """Parse the launcher's immutable snapshot with an app's argparse program.

    The invoked command's handler runs on a successful parse; app code there
    publishes its service and rejects an invalid invocation with
    ``program.error(...)`` (whose ``SystemExit(2)`` routes through
    ``appExit``). This helper has no plugin-row or service ownership
    semantics.

    Help, version, and rejected arguments — from the grammar or from a
    handler — are terminal for the process: argparse writes the text and the
    helper requests ``appExit``. The handler never runs on help, version, or
    a grammar rejection; a handler must reject before it publishes, because
    statements before its ``program.error(...)`` have already run.

    :param services: plugin services carrying ``cmdlineArgs`` and ``appExit``.
    :param program: the app's argparse program with flags and handlers.
    :param handler: optional explicit handler; defaults to the tree's
        ``cmdline_handler`` default chosen by the successful parse.
    :returns: the handler's result on a successful parse, or None when the
        parse was terminal (help/version/rejection).
    :raises RuntimeError: when the launcher did not provide the command line
        and exit request, or when no command in the program declares a
        handler.
    """
    args = services.get(CMDLINE_ARGS_KEY)
    exit_request = services.get(APP_EXIT_KEY)
    name = getattr(program, "prog", None) or "program"
    if args is None or exit_request is None:
        raise RuntimeError(
            f"{name}: the launcher must provide ctx.cmdlineArgs and "
            f"ctx.appExit before the tree mounts")
    if handler is None and not has_action(program):
        raise RuntimeError(
            f"{name}: no command in the program declares an action; "
            f"parse_cmdline runs the invoked command's action on a successful "
            f"parse, and app code there publishes its service")
    saved_out, saved_err = sys.stdout, sys.stderr
    sys.stdout = internals["stdout"]
    sys.stderr = internals["stderr"]
    try:
        namespace = program.parse_args(list(args.get()))
    except SystemExit as error:
        # commander's control-flow errors (help, version, a parse error, or
        # program.error) are terminal for the process: write nothing more,
        # route the code through the launcher's bounded exit request.
        code = error.code
        if isinstance(code, bool):
            exit_request(1 if code else 0)
        elif isinstance(code, int):
            exit_request(code)
        elif code is None:
            exit_request(0)
        else:
            exit_request(1)
        return None
    finally:
        sys.stdout, sys.stderr = saved_out, saved_err
    resolved = handler if handler is not None else getattr(
        namespace, "cmdline_handler", None)
    if resolved is None:  # pragma: no cover — has_action guards this
        raise RuntimeError(f"{name}: the parsed command declares no handler")
    try:
        return resolved(namespace)
    except SystemExit as error:
        # the handler's program.error(...) analogue: an action must reject
        # before it publishes, and the rejection is terminal through appExit
        code = error.code
        if isinstance(code, bool):
            exit_request(1 if code else 0)
        elif isinstance(code, int):
            exit_request(code)
        elif code is None:
            exit_request(0)
        else:
            exit_request(1)
        return None

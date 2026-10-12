"""Native port of the remaining @deepseek-ai/dsh-app-boot contract face
(commit 5badb150, lib/index.js 4183-line bundle + lib/worker read; the boot
reconciliation half already lives in runtime/plugin_host.py per the B1 core
loop round — see the plan migration_note).

This module carries the three contract blocks the plan's remaining_contract
names, plus the shared startup-audit face:

1. **.env loading** — ``load_env`` (the single-file launcher convenience),
   ``load_layered_env`` (inherited > invoking-directory > Harness-home with
   the bootstrap-only refusal list), ``parse_env`` (Node v24
   ``util.parseEnv`` semantics, pinned by 28 real Electron-host probes), and
   the verbatim ``BOOTSTRAP_NAMES`` / ``BOOTSTRAP_PREFIXES`` /
   ``HOME_LAYER_PROXY_NAMES`` policy sets. A proxy chooses the route every
   request takes, so only the user's own home file may set the proxy names;
   the CA and TLS names stay refused everywhere because they change what is
   trusted, not where traffic goes.
2. **fail-loud guards** — ``install_fail_loud`` (any uncaught exception at
   any point in the process lifetime becomes one labelled stderr diagnostic
   and a hard exit(1); a latch keeps the first failure the reported one; an
   optional release hook is awaited under ``FAIL_LOUD_RELEASE_TIMEOUT_MS``
   whose timer stays referenced so a wedged disposer delays the fatal exit
   but never cancels it), the assembled-rejection exemption window, and
   ``StartupError`` with its aggregate cause.
3. **snapshot-aware config resolution** — ``resolve_config_path`` (replay
   swaps a ``cordis.yml`` basename for ``cordis.snapshot.yml`` in the same
   directory; every other mode keeps the path).
4. **startup audit** — ``inactive_entries`` / ``audit_startup_entries`` /
   the diagnostic renderers, adapted from Cordis Loader fibers to the
   PluginHost phase vocabulary (the fiber-state mirrors become phase
   reads; the pending-missing-services list becomes unsatisfied
   dependencies). ``add_harness_source_section`` augments the shared
   SystemPrompt service with the verbatim harness-source section text.

Subset boundary (documented, not faked): the profile machinery
(loadProfile/loadProfileDirectory/readProfilePatches/reconcileProfilePatches/
composeEntries/renderConfigDump/initProfile/sanitizeProfile and friends),
the profile-resolution resolver + worker, the config-schema projector face,
and the plugin-compatibility evaluator are Cordis Loader tree / profile
directory composition semantics with no consumer shape on this host
(PluginHost is builtin-seeded with a config face, not a profile directory).
They stay deferred with the dsh-hmr / dsh-config-editor profile faces that
consume them; the deferral is recorded in the plan migration_note and the
ledger.

Python adaptation notes:
- ``process.env`` mutation → ``os.environ`` mutation (same process-global
  semantics); the returned snapshot is the R5 launch-environment snapshot
  built from the real layers.
- ``process.loadEnvFile`` semantics (probed on the real Electron-host Node
  v24.17.0): parse with ``util.parseEnv`` rules, apply without replacing
  already-present variables, throw with ``code=ENOENT`` for a missing file.
- ``util.inspect(err, {depth: 4})`` → repr chain over ``__cause__`` /
  ``__context__`` / ExceptionGroup members plus the exception's own
  attribute dict: the enumerable-property rationale (an fs error's code,
  syscall, path and cause chain are what a crash report needs, and once a
  handler is installed nothing else records the failure) carries over.
- ``unhandledRejection`` has no Python analogue (Python exceptions are
  synchronous); the single label ``fatal uncaught exception`` covers the
  interpreter hook and the threading hook. The assembled-rejection
  exemption window is ported verbatim for future reconciliation consumers.
- ``proc.exit(1)`` → ``os._exit(1)``: Python's interpreter would otherwise
  keep serving after a *thread* exception, while the upstream contract is
  that control never returns to the failed operation — only the throw site
  knows which state is intact.
- ``setImmediate`` rejection checkpoint → a thread yield (``time.sleep(0)``);
  the retain/release counting is verbatim.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
import traceback
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set

from AstralNexus.runtime.compat import ExceptionGroup
from AstralNexus.runtime.dshutil.home_paths import dsh_home_path, resolve_dsh_home
from AstralNexus.runtime.dshutil.launch_environment import (
    create_launch_environment_snapshot,
)

# --------------------------------------------------------------------------
# snapshot-aware config resolution
# --------------------------------------------------------------------------

#: Basename pattern the replay mode swaps (verbatim upstream regex).
_SNAPSHOT_BASENAME_PATTERN = re.compile(r"cordis\.ya?ml$")


def resolve_config_path(config_path: str, snapshot_mode: Optional[str],
                        cwd: Optional[str] = None) -> str:
    """Resolve the config to boot.

    Replay swaps a ``cordis.yml`` basename for ``cordis.snapshot.yml`` in
    the same directory; every other mode keeps the path.

    :param config_path: the requested config path (absolute, or relative to
        ``cwd``).
    :param snapshot_mode: the bin's ``$DSH_SNAPSHOT`` value; only ``replay``
        swaps the basename.
    :param cwd: the base a relative ``config_path`` resolves against.
    :returns: the absolute path of the config to boot.
    """
    base = os.getcwd() if cwd is None else cwd
    absolute = os.path.abspath(os.path.join(base, config_path))
    if snapshot_mode != "replay":
        return absolute
    replaced = _SNAPSHOT_BASENAME_PATTERN.sub(
        "cordis.snapshot.yml", os.path.basename(absolute))
    return os.path.join(os.path.dirname(absolute), replaced)


# --------------------------------------------------------------------------
# .env parsing (Node v24 util.parseEnv semantics, probe-pinned)
# --------------------------------------------------------------------------

def parse_env(text: str) -> Dict[str, str]:
    """Parse ``.env`` text into a mapping without applying it.

    Semantics pinned by real probes against the Electron-host Node
    v24.17.0 ``util.parseEnv`` (28 cases; see the wave test file):
    ``KEY=VALUE`` lines; blank lines and ``#`` comment lines skipped; an
    ``export `` prefix accepted; keys and unquoted values trimmed (tabs
    included); the first ``=`` splits; unquoted values end at the first
    ``#`` (no preceding space required) or the line end; ``"``/``'`` quoted
    values keep inner spaces, ``#`` and newlines verbatim with no backslash
    escapes (the first matching closing quote ends the value and the rest
    of the line is ignored); an unclosed quote falls back to unquoted
    treatment from the quote character; duplicate keys take the later
    value without error; ``$`` is never expanded; a BOM is not stripped and
    becomes part of the first key; lines without ``=`` or with an empty key
    are ignored.
    """
    values: Dict[str, str] = {}
    position = 0
    length = len(text)
    while position < length:
        while position < length and text[position] in " \t":
            position += 1
        if position >= length:
            break
        char = text[position]
        if char in "\r\n":
            position += 1
            continue
        if char == "#":  # whole-line comment
            end = text.find("\n", position)
            position = length if end < 0 else end + 1
            continue
        line_end = text.find("\n", position)
        if line_end < 0:
            line_end = length
        equals = text.find("=", position, line_end)
        if equals < 0:  # a line without an assignment is ignored
            position = length if line_end >= length else line_end + 1
            continue
        key = text[position:equals].strip()
        if key.startswith("export ") or key.startswith("export\t"):
            key = key[len("export"):].lstrip(" \t")
        position = equals + 1
        while position < length and text[position] in " \t":
            position += 1
        value: str
        if position < length and text[position] in "\"'":
            quote = text[position]
            close = text.find(quote, position + 1)
            if close >= 0:
                value = text[position + 1:close]
                position = close + 1
                end = text.find("\n", position)  # rest of line ignored
                position = length if end < 0 else end + 1
            else:
                # unclosed quote: unquoted treatment from the quote itself
                end = text.find("\n", position)
                raw_end = length if end < 0 else end
                raw = text[position:raw_end]
                hash_at = raw.find("#")
                if hash_at >= 0:
                    raw = raw[:hash_at]
                value = raw.rstrip()
                position = length if end < 0 else end + 1
        else:
            end = text.find("\n", position)
            raw_end = length if end < 0 else end
            raw = text[position:raw_end]
            hash_at = raw.find("#")
            if hash_at >= 0:
                raw = raw[:hash_at]
            value = raw.rstrip()
            position = length if end < 0 else end + 1
        if key:
            values[key] = value
    return values


# --------------------------------------------------------------------------
# bootstrap-only refusal policy (verbatim upstream sets)
# --------------------------------------------------------------------------

#: Exact names no discovered file may set.
BOOTSTRAP_NAMES: Set[str] = {
    "PATH", "HOME", "USERPROFILE", "SHELL", "NODE_OPTIONS", "NODE_PATH",
    "NODE_EXTRA_CA_CERTS", "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS", "PERL5OPT", "PERL5LIB",
    "PYTHONSTARTUP", "PYTHONPATH", "RUBYOPT", "RUBYLIB", "JAVA_TOOL_OPTIONS",
    "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS", "PYTHONHOME", "GIT_SSH",
    "GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_EDITOR",
    "GIT_ASKPASS", "SSH_ASKPASS", "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM",
    "GIT_CONFIG_COUNT", "EDITOR", "VISUAL", "PAGER", "BROWSER",
    "DEEPSEEK_BASE_URL", "DEEPSEEK_SEARCH_BASE_URL", "SSL_CERT_FILE",
    "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_TLS_REJECT_UNAUTHORIZED",
}

#: Name prefixes no discovered file may set.
BOOTSTRAP_PREFIXES: Sequence[str] = ("DSH_", "XDG_", "DYLD_", "BASH_FUNC_")

#: The bootstrap names the Harness-home `.env` alone may set. A proxy
#: chooses the route every request takes, so the invoking directory's file —
#: which arrives with a clone — keeps refusing them; the home file is the
#: user's own, and ``DSH_HOME`` is itself bootstrap-only, so no ``.env`` can
#: relocate this exemption. The CA and TLS names in the same group stay
#: refused everywhere: they change what is trusted, not where traffic goes.
HOME_LAYER_PROXY_NAMES: Set[str] = {
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
}


def is_bootstrap_only(name: str) -> bool:
    """Whether only the inherited process environment may supply ``name``."""
    upper = name.upper()
    if upper in BOOTSTRAP_NAMES:
        return True
    return any(upper.startswith(prefix) for prefix in BOOTSTRAP_PREFIXES)


def _default_warn(line: str) -> None:
    sys.stderr.write(line)


def read_env_layer(bin_name: str, directory: str,
                   warn: Callable[[str], None] = _default_warn,
                   home: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Parse one directory's ``.env`` without applying it.

    Bootstrap-only names are rejected before any value is materialized.

    :param bin_name: the diagnostic prefix on the thrown error.
    :param directory: the directory whose ``.env`` to read.
    :param warn: sink for the one-line unreadable-file diagnostic.
    :param home: the resolved Harness home; when ``directory`` is it, the
        home-layer proxy names are accepted.
    :returns: ``{"path", "values"}``, or None when absent or unreadable.
    :raises OSError-free RuntimeError-free ``Error``: when the file declares
        a name :func:`is_bootstrap_only` rejects and this layer may not set
        it (message carries the remedy, verbatim upstream text).
    """
    path = os.path.join(directory, ".env")
    resolved_home = resolve_dsh_home() if home is None else home
    is_home = os.path.abspath(directory) == os.path.abspath(resolved_home)
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            content = handle.read()
    except FileNotFoundError:
        return None
    except OSError as error:
        warn(f"{bin_name}: failed to load .env: {error}\n")
        return None
    values = parse_env(content)
    for name in values:
        if not is_bootstrap_only(name):
            continue
        proxy_name = name.upper() in HOME_LAYER_PROXY_NAMES
        if is_home and proxy_name:
            continue
        home_env = os.path.join(resolved_home, ".env")
        remedy = (
            f"export {name}, or put it in {home_env}, which does not travel "
            f"with a repository" if proxy_name else
            f"export {name} instead of putting it in a .env file")
        raise RuntimeError(
            f'{bin_name}: {path} sets "{name}", which only the launching '
            f"environment may set (it decides how this process starts, where "
            f"its code and instructions load from, or how it reaches the "
            f"network); {remedy}")
    return {"path": path, "values": values}


def load_env(bin_name: str, directory: Optional[str] = None,
             warn: Callable[[str], None] = _default_warn) -> None:
    """Load the optional gitignored ``.env`` from ``directory``.

    Missing files fall back to the ambient environment; accepted values are
    applied without replacing already-present ones (probed
    ``process.loadEnvFile`` semantics); other read failures are reported
    through ``warn``.
    """
    base = os.getcwd() if directory is None else directory
    path = os.path.join(base, ".env")
    try:
        with open(path, "r", encoding="utf-8", newline="") as handle:
            content = handle.read()
    except FileNotFoundError:
        return
    except OSError as error:
        warn(f"{bin_name}: failed to load .env: {error}\n")
        return
    for name, value in parse_env(content).items():
        if name not in os.environ:
            os.environ[name] = value


def load_layered_env(bin_name: str, cwd: Optional[str] = None,
                     warn: Callable[[str], None] = _default_warn
                     ) -> Any:
    """Load inherited > invoking-directory > Harness-home ``.env`` layers.

    The Harness home resolves before either file; both files are checked
    before either is applied, and accepted values are materialized without
    replacing inherited ones. The snapshot preserves which layer supplied
    each value.

    :raises RuntimeError: when either file declares a bootstrap-only
        variable, except the home-layer proxy names in the Harness-home
        file.
    """
    invoking = os.getcwd() if cwd is None else cwd
    home = resolve_dsh_home()
    inherited = dict(os.environ)
    project = read_env_layer(bin_name, invoking, warn, home)
    user = (None if os.path.abspath(home) == os.path.abspath(invoking)
            else read_env_layer(bin_name, home, warn, home))
    for layer in (project, user):
        if layer is None:
            continue
        for name, value in layer["values"].items():
            if name not in os.environ:
                os.environ[name] = value
    layers: List[Dict[str, Any]] = [{"source": "process", "values": inherited}]
    if project is not None:
        layers.append({"source": "project-env", "path": project["path"],
                       "values": project["values"]})
    if user is not None:
        layers.append({"source": "user-env", "path": user["path"],
                       "values": user["values"]})
    return create_launch_environment_snapshot(layers)


# --------------------------------------------------------------------------
# fail-loud guards
# --------------------------------------------------------------------------

#: How long :func:`install_fail_loud` waits for its release hook before
#: exiting anyway. A wedged disposer must delay the fatal exit, never
#: cancel it.
FAIL_LOUD_RELEASE_TIMEOUT_MS = 2000

_assembled_activation_rejections: Dict[int, int] = {}


def retain_assembled_rejection(reason: BaseException) -> None:
    """Hold one assembled rejection through its process checkpoint."""
    key = id(reason)
    _assembled_activation_rejections[key] = \
        _assembled_activation_rejections.get(key, 0) + 1


def release_assembled_rejection(reason: BaseException) -> None:
    """Release one hold; the last release removes the exemption."""
    key = id(reason)
    count = _assembled_activation_rejections.get(key)
    if count is None or count == 1:
        _assembled_activation_rejections.pop(key, None)
    else:
        _assembled_activation_rejections[key] = count - 1


def observe_loader_rejection_checkpoint(reasons: Iterable[BaseException]) -> None:
    """Let already-scheduled duplicate notifications arrive, exempted.

    The ``setImmediate`` checkpoint becomes a thread yield; the
    retain/release counting around it is verbatim.
    """
    held = list(reasons)
    for reason in held:
        retain_assembled_rejection(reason)
    try:
        time.sleep(0)
    finally:
        for reason in held:
            release_assembled_rejection(reason)


def _format_fatal_detail(error: BaseException) -> str:
    """inspect(err, {depth: 4}) analogue: repr chain + enumerable attrs.

    A filesystem error's code, syscall and path and any cause chain are the
    enumerable properties a crash report needs; once a handler is installed
    the runtime prints nothing of its own, so this line is the only record
    of the failure.
    """
    parts: List[str] = []
    seen: Set[int] = set()

    def visit(value: Any) -> None:
        if isinstance(value, BaseException):
            if id(value) in seen:
                return
            seen.add(id(value))
            parts.append(repr(value))
            attributes = {key: repr(item) for key, item in vars(value).items()}
            if isinstance(value, OSError):
                # an OS error's code/syscall/path equivalents live in C-level
                # slots that vars() does not see — exactly the properties a
                # crash report needs, so read them through getattr
                for key in ("errno", "strerror", "filename", "filename2",
                            "winerror"):
                    item = getattr(value, key, None)
                    if item is not None and key not in attributes:
                        attributes[key] = repr(item)
            if attributes:
                parts.append(f"  attributes: {attributes}")
            members = getattr(value, "exceptions", None)
            if members is not None:  # ExceptionGroup analogue of AggregateError
                for member in members:
                    visit(member)
            if value.__cause__ is not None:
                parts.append("  cause:")
                visit(value.__cause__)
            elif value.__context__ is not None and not value.__suppress_context__:
                parts.append("  context:")
                visit(value.__context__)
        else:
            parts.append(str(value))

    visit(error)
    return "\n".join(parts)


def install_fail_loud(bin_name: str,
                      proc: Optional[Dict[str, Any]] = None,
                      release: Optional[Callable[[], Any]] = None
                      ) -> Callable[[], None]:
    """Turn any uncaught exception into one labelled diagnostic and exit(1).

    Install before boot. A rejection already included by the startup audit
    is ignored during its process checkpoint; every other uncaught
    exception (interpreter hook and threading hook alike) remains fatal.
    Control never returns to the failed operation: only the throw site
    knows which state is intact, and a listener that threw mid-update
    leaves silently wrong results behind if it were resumed. The process
    keeps running only until the release hook settles or times out.
    Stdout remains untouched for ACP; the returned function removes both
    handlers.

    The handler stays installed while the release runs — removing it would
    let a second concurrent failure become uncaught and kill the process
    mid-teardown, stranding exactly the state the release restores — so a
    latch keeps the first failure the reported one and lets later failures
    (including the release's own) fall through to the pending exit. The
    diagnostic is written before the release so a hanging or failing
    disposer cannot swallow the reason.

    :param bin_name: the diagnostic prefix on the fatal-failure line.
    :param proc: the process slice to register on; tests inject a fake with
        ``write_stderr``/``exit`` callables.
    :param release: optional teardown awaited before exit, used by a
        terminal-owning surface to restore the terminal. Its own failure is
        swallowed because the pending fatal exit already owns the outcome.
    :returns: the uninstaller that removes both handlers.
    """
    process: Dict[str, Callable[..., Any]] = proc if proc is not None else {
        "write_stderr": lambda line: sys.stderr.write(line),
        "exit": os._exit,
    }
    state = {"exiting": False}

    def report(error: BaseException, label: str) -> None:
        if state["exiting"]:
            return  # the latch keeps the first failure the reported one
        state["exiting"] = True
        process["write_stderr"](
            f"{bin_name}: {label}: {_format_fatal_detail(error)}\n")
        if release is None:
            process["exit"](1)
            return
        done = threading.Event()

        def run_release() -> None:
            try:
                release()
            except BaseException:
                pass  # the pending fatal exit already owns the outcome
            finally:
                done.set()

        worker = threading.Thread(target=run_release,
                                  name="fail-loud-release", daemon=True)
        worker.start()
        # the timer stays referenced: a never-settling disposer cannot let
        # the process reach a clean exit instead of failing
        done.wait(FAIL_LOUD_RELEASE_TIMEOUT_MS / 1000)
        process["exit"](1)

    def on_exception(exc_type: Any, exc_value: BaseException,
                     exc_tb: Any) -> None:
        if id(exc_value) in _assembled_activation_rejections:
            return
        report(exc_value, "fatal uncaught exception")

    def on_thread_exception(args: Any) -> None:
        exc_value = getattr(args, "exc_value", None)
        if exc_value is None:
            return
        if id(exc_value) in _assembled_activation_rejections:
            return
        report(exc_value, "fatal uncaught exception")

    saved = (sys.excepthook, threading.excepthook)
    sys.excepthook = on_exception
    threading.excepthook = on_thread_exception

    def uninstall() -> None:
        sys.excepthook, threading.excepthook = saved

    return uninstall


# --------------------------------------------------------------------------
# startup audit (PluginHost phase adaptation of the Loader fiber audit)
# --------------------------------------------------------------------------

#: Value mirrors of the phase vocabulary (PluginHost's persisted phases are
#: the fiber-state analogues; kept aligned with runtime/plugin_host.py).
PHASE_PENDING = "pending"
PHASE_ACTIVE = "active"
PHASE_FAILED = "failed"
PHASE_DISPOSED = "disposed"

#: Entry ids whose presence defines a usable DSH application (verbatim
#: upstream list; the bundle's audit treats every inactive entry as
#: rejecting — the list is exported for bin-layer policy and kept here as
#: the composition vocabulary).
REQUIRED_STARTUP_ENTRY_IDS: Set[str] = {
    "agent-loop", "webserver", "modules", "connection", "headless-runner",
    "acp", "sdk-jsonrpc-server",
}


class StartupError(Exception):
    """Startup audit failure with metadata and original failures as cause."""

    def __init__(self, message: str, entries: List[Dict[str, Any]]) -> None:
        # ExceptionGroup admits Exception subtypes only; a BaseException
        # that is not one (KeyboardInterrupt, SystemExit) is rendered into
        # a RuntimeError so the aggregate keeps every original failure
        failures: List[Exception] = []

        def collect(value: Any) -> None:
            if isinstance(value, Exception):
                failures.append(value)
            elif isinstance(value, BaseException):
                failures.append(RuntimeError(f"{type(value).__name__}: {value}"))
            else:
                failures.append(RuntimeError(str(value)))

        for row in entries:
            outcome = row.get("outcome") or {}
            if outcome.get("kind") == "failed" and outcome.get("error") is not None:
                collect(outcome["error"])
            failure = row.get("failure")
            if failure is not None:
                collect(failure)
        super().__init__(message)
        self.entries = entries
        #: Root configuration and startup logs, attached by the boot owner
        #: after disposal.
        self.startup: Optional[Dict[str, Any]] = None
        if failures:
            self.__cause__ = ExceptionGroup(
                "Plugin activation failures", failures)


def format_activation_error(error: Any) -> str:
    """Render exception chains, nested causes, and group members once."""
    details: List[str] = []
    seen: Set[int] = set()

    def visit(value: Any) -> None:
        if not isinstance(value, BaseException):
            details.append(str(value))
            return
        if id(value) in seen:
            return
        seen.add(id(value))
        rendered = "".join(traceback.format_exception(
            type(value), value, value.__traceback__)).rstrip()
        details.append(rendered or str(value))
        members = getattr(value, "exceptions", None)
        if members is not None:
            for member in members:
                visit(member)
        if value.__cause__ is not None:
            visit(value.__cause__)

    visit(error)
    return "\n".join(details)


def inactive_entries(host: Any) -> List[Dict[str, Any]]:
    """Collect activation failures from a settled PluginHost.

    Fiber-state reads become phase reads: a disabled (desired=disabled)
    entry is skipped like upstream's ``entry.disabled``; ``failed`` yields a
    failed outcome carrying ``last_error``; ``pending``/``loading`` yield a
    pending outcome whose missing list is the unsatisfied dependencies;
    ``disposed`` with desired=enabled means the reconcile never activated
    the entry (a failed activation whose phase settled back), reported as
    failed with the recorded error.
    """
    failures: List[Dict[str, Any]] = []
    for entry in host.inventory():
        if entry.get("desired") != "enabled":
            continue  # disabled entries do not affect startup
        phase = entry.get("phase")
        if phase == PHASE_ACTIVE:
            continue
        if phase in (PHASE_PENDING, "loading"):
            active_ids = {row["id"] for row in host.inventory()
                          if row.get("phase") == PHASE_ACTIVE}
            missing = [dep for dep in entry.get("dependencies", [])
                       if dep not in active_ids]
            failures.append({"entry": entry,
                             "outcome": {"kind": "pending",
                                         "missing": missing}})
        else:
            error = entry.get("last_error") or "failed to activate"
            failures.append({"entry": entry,
                             "outcome": {"kind": "failed", "error": error}})
    return failures


def failure_detail(outcome: Dict[str, Any]) -> str:
    """Render one failed plugin's original error and activation phase."""
    phase = outcome.get("phase")
    prefix = f"{phase}: " if phase else ""
    return f"{prefix}{format_activation_error(outcome['error'])}"


def inactive_diagnostic(row: Dict[str, Any]) -> str:
    """Stable per-entry text for reload comparisons and optional warnings."""
    entry = row["entry"]
    outcome = row["outcome"]
    name = entry.get("name") or entry.get("id")
    if outcome["kind"] == "failed":
        detail = failure_detail(outcome)
    else:
        missing = outcome["missing"]
        noun = "service" if len(missing) == 1 else "services"
        detail = (f"pending (waiting for {noun}: "
                  f"{', '.join(missing) or 'unknown'})")
    return f"{entry['id']} ({name}): {detail}"


def activation_diagnostic(bin_name: str,
                          failures: List[Dict[str, Any]]) -> str:
    """Render optional-only warnings without changing startup policy."""
    noun = "entry" if len(failures) == 1 else "entries"
    rendered = "\n".join(inactive_diagnostic(row) for row in failures)
    return (f"{bin_name}: warning: {len(failures)} {noun} did not activate\n"
            f"{rendered}\n")


def startup_diagnostic(bin_name: str, failures: List[Dict[str, Any]],
                       required: Sequence[str]) -> str:
    """Group startup failures and pending services, marking required ones.

    ``required`` is the list of required entry ids (the JS Set's reference
    identity becomes id comparison).
    """
    noun = "plugin" if len(required) == 1 else "plugins"
    lines = [f"{bin_name}: startup failed: {len(required)} required {noun} "
             f"did not activate"]
    failed = [row for row in failures if row["outcome"]["kind"] == "failed"]
    pending = [row for row in failures if row["outcome"]["kind"] == "pending"]
    pending.sort(key=lambda row: 0 if row["entry"]["id"] in required else 1)

    def label(row: Dict[str, Any]) -> str:
        suffix = " (required)" if row["entry"]["id"] in required else ""
        return f"{row['entry']['id']}{suffix}"

    if failed:
        lines.extend(["", f"Failed plugins ({len(failed)}):"])
        for row in failed:
            lines.append(f"  {label(row)}")
            lines.append(f"    Package: {row['entry'].get('name')}")
            lines.extend(f"    {line}"
                         for line in failure_detail(row["outcome"]).split("\n"))
    if pending:
        width = max([6] + [len(label(row)) for row in pending]) + 2
        lines.extend(["", f"Plugins waiting for services ({len(pending)}):",
                      f"  {'Plugin'.ljust(width)}Missing services"])
        for row in pending:
            missing = row["outcome"]["missing"]
            lines.append(f"  {label(row).ljust(width)}"
                         f"{', '.join(missing) or 'unknown'}")
    return "\n".join(lines)


def audit_startup_entries(host: Any, bin_name: str,
                          warn: Callable[[str], None] = _default_warn) -> None:
    """Apply DSH startup policy to a settled PluginHost.

    Inactive entries reject startup (the bundle's audit treats every
    enabled-but-inactive entry as required); the thrown
    :class:`StartupError` carries the per-entry metadata. The warn branch
    mirrors upstream's optional-only diagnostic for callers that audit a
    deliberately partial tree.

    :raises StartupError: when any enabled entry is inactive.
    """
    failures = inactive_entries(host)
    required = [row["entry"]["id"] for row in failures]
    if failures:
        entries = []
        for row in failures:
            entry = row["entry"]
            outcome = row["outcome"]
            if outcome["kind"] == "pending":
                missing = outcome["missing"]
                noun = "service" if len(missing) == 1 else "services"
                failure: Any = RuntimeError(
                    f"pending (waiting for {noun}: "
                    f"{', '.join(missing) or 'unknown'})")
            else:
                failure = None
            entries.append({
                "id": entry["id"],
                "module": entry.get("name"),
                "required": True,
                "phase": entry.get("phase"),
                "outcome": outcome,
                "failure": failure,
            })
        raise StartupError(
            startup_diagnostic(bin_name, failures, required), entries)
    # no failures: nothing to warn about (kept for the upstream call shape)


def audit_startup_warnings(host: Any, bin_name: str,
                           warn: Callable[[str], None] = _default_warn
                           ) -> List[str]:
    """Non-fatal audit face for a persistent host.

    The upstream ``boot`` rejects the whole process when any enabled entry
    is inactive; a persistent workbench host instead surfaces per-plugin
    failures through its status face and keeps successful siblings running.
    This function renders the identical diagnostics through ``warn`` and
    returns them, so the startup failure text stays available without
    turning one bad plugin into a dead host (documented adaptation; the
    rejecting face above remains the bin-shaped contract).
    """
    failures = inactive_entries(host)
    if not failures:
        return []
    diagnostic = activation_diagnostic(bin_name, failures)
    warn(diagnostic)
    return [inactive_diagnostic(row) for row in failures]


# --------------------------------------------------------------------------
# harness-source prompt section
# --------------------------------------------------------------------------

#: Prompt-section name for the harness-source location line an app bin adds
#: after boot (verbatim).
HARNESS_SOURCE_SECTION = "harness:source"

#: Verbatim upstream section text.
HARNESS_SOURCE_TEXT_TEMPLATE = (
    "The DeepSeek Harness implementation checkout is at {source_root}. The "
    "checkout location and current working directory are separate values and "
    "may differ; never infer the working directory from this path. Use pwd "
    "to determine the current working directory. Use this checkout only to "
    "inspect or extend DSH itself.")


def add_harness_source_section(system_prompt: Any,
                               source_root: str) -> Optional[Callable[[], None]]:
    """Add the global prompt section naming the on-disk harness checkout.

    The section explicitly distinguishes the checkout from the task
    workspace and current working directory; the self-referential
    ``dsh-tool-cordis`` toolset reads and edits this checkout. Call once on
    the settled boot surface; the section uses the shared first-party
    placement after reusable instructions and before the Web surface and
    persona suffix. A booted tree with no ``systemPrompt`` service has no
    prompt to augment, so this is then a no-op that returns None.

    :param system_prompt: the mounted SystemPrompt service, or None.
    :param source_root: the absolute path to the harness checkout root.
    :returns: the section disposer, or None when no prompt is mounted.
    """
    if system_prompt is None:
        return None
    return system_prompt.section({
        "name": HARNESS_SOURCE_SECTION,
        "order": system_prompt.get_section_order("HARNESS_SOURCE"),
        "text": HARNESS_SOURCE_TEXT_TEMPLATE.format(source_root=source_root),
    })


__all__ = [
    "BOOTSTRAP_NAMES",
    "BOOTSTRAP_PREFIXES",
    "FAIL_LOUD_RELEASE_TIMEOUT_MS",
    "HARNESS_SOURCE_SECTION",
    "HARNESS_SOURCE_TEXT_TEMPLATE",
    "HOME_LAYER_PROXY_NAMES",
    "REQUIRED_STARTUP_ENTRY_IDS",
    "StartupError",
    "activation_diagnostic",
    "add_harness_source_section",
    "audit_startup_entries",
    "audit_startup_warnings",
    "dsh_home_path",
    "failure_detail",
    "format_activation_error",
    "inactive_diagnostic",
    "inactive_entries",
    "install_fail_loud",
    "is_bootstrap_only",
    "load_env",
    "load_layered_env",
    "observe_loader_rejection_checkpoint",
    "parse_env",
    "read_env_layer",
    "release_assembled_rejection",
    "resolve_config_path",
    "retain_assembled_rejection",
    "startup_diagnostic",
]

"""Tool-call loop guardrails for a single LLM execution."""
from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping

from .verification_identity import (
    changed_premises,
    command_of_arguments,
    is_guard_blocked,
    verification_identity,
)


class ToolCallGuard:
    """Detect unchanged tool-call loops without blocking normal retries.

    Two independent budgets bound repeated calls:

    * failure budget (``max_repeats``/``failure_threshold``) — unchanged
      semantics: repeated failures escalate, successful results clear them.
    * read-stall budget (``read_streak_limit``) — successful ``file_read``
      calls of an unchanged content version accumulate a streak that is only
      reset by explicit action tools such as ``file_write``/``pytest_run``.
      Generic command success is not evidence of progress: Python and shell
      commands may only print the same files. A worker that keeps re-reading the
      same unchanged files without ever writing or executing is in a livelock
      (observed on r11 N-007: a strict 5-file read cycle repeated 15+ times
      with zero writes), and exceeding the budget halts the loop through the
      existing decision path.  Legitimate post-compression re-reads stay
      allowed because progressing workers reset the streak, and the default
      budget (8) keeps the documented "successful re-reads do not consume the
      failure budget" contract intact.
    """

    #: Tools whose successful calls never count as observable progress.
    NON_PROGRESS_TOOLS = frozenset({
        "file_read", "search_files", "glob_files", "runtime_dependencies",
        "sqlite_query", "http_get", "web_search", "skills_list", "skill_view",
        "runtime_plugins", "mcp_servers", "mcp_tools", "lsp_query",
        "runtime_inspect", "workflow_status", "schedule_list", "session_history",
        "find_skills", "skill_list", "skill_audit", "browser_fetch",
        "arxiv_search", "crossref_search", "openalex_search", "github_repo_contents",
    })
    # Do not infer effects from argv/source text or stdout (including a printed
    # "passed" or "file changed"). Changed file versions already create fresh
    # read identities. This does not restrict execution of these tools.
    GENERIC_EXECUTION_TOOLS = frozenset({"python_exec", "terminal_exec"})

    def __init__(self, max_repeats: int | None = None, failure_threshold: int | None = None, workspace_root: str | Path | None = None, read_streak_limit: int | None = None, premise_provider: Callable[[], Dict[str, str]] | None = None, verification_commands: Mapping[str, str] | None = None) -> None:
        self.max_repeats = max_repeats or max(1, int(os.environ.get("ASTRALNEXUS_TOOL_LOOP_REPEATS", "3")))
        self.failure_threshold = failure_threshold or max(
            1, int(os.environ.get("ASTRALNEXUS_TOOL_FAILURE_THRESHOLD", "2"))
        )
        self.read_streak_limit = read_streak_limit or max(
            1, int(os.environ.get("ASTRALNEXUS_TOOL_READ_STREAK_LIMIT", "8"))
        )
        self.workspace_root = Path(workspace_root).resolve() if workspace_root is not None else None
        self._calls: Counter[str] = Counter()
        self._failures: Counter[str] = Counter()
        self._read_keys: set[str] = set()
        self._read_streaks: Counter[str] = Counter()
        # Fix AT: the read-stall budget above is keyed on the exact call, so it
        # only accumulates when a worker repeats an IDENTICAL read.  Fix AP gave
        # file_read windows (offset / start_line / end_line); paging spreads the
        # same file across many fingerprints and the streak never reaches the
        # limit.  Live r13b: PATCH-R017 read its own 47.5 KB run_acceptance.py
        # 123 times in one execution (gen 26) and 17 times in 20 minutes (gen 27)
        # without a single write or execution, and the stall budget -- written for
        # exactly this livelock -- never fired.  These two track the same budget
        # per (path, content version), counting only windows already returned, so
        # honest forward paging costs nothing and re-reading what you already hold
        # costs what it always did.
        self._read_windows: dict[str, set[tuple[int, int]]] = {}
        self._stall_streaks: Counter[str] = Counter()
        # Fix r29 (doc 51 §16): a repeat budget alone cannot tell "the worker is
        # looping" from "the worker must re-run the anchored command because the
        # premises it was verified against changed".  The execution contract
        # itself requires the re-run after a tracked-file edit, yet the fourth
        # identical call was refused and the refusal was recorded as a receipt
        # the gate then anchored on.  With a premise provider the guard records
        # the premise versions observed right AFTER each attempt and grants one
        # re-verification when they changed since; with ``premise_provider=None``
        # (the default, and every existing caller) behaviour is unchanged.
        self.premise_provider = premise_provider
        self.verification_commands = dict(verification_commands or {})
        self._premise_at_last_attempt: Dict[str, Dict[str, str]] = {}
        self._last_call_id: Dict[str, str] = {}
        self._granted_attempt: Dict[str, int] = {}
        self._guard_blocks: Counter[str] = Counter()
        self._reverification_grants: Counter[str] = Counter()
        self._reverification_log: List[Dict[str, Any]] = []

    @staticmethod
    def _tool_name(call: Dict[str, Any]) -> str:
        return str((call.get("function") or {}).get("name", ""))

    @staticmethod
    def fingerprint(call: Dict[str, Any]) -> str:
        function = call.get("function") or {}
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                pass
        payload = {
            "name": function.get("name", ""),
            "arguments": arguments,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:20]

    def _referenced_file_versions(self, arguments: Dict[str, Any]) -> Dict[str, str]:
        """Content hashes of workspace files this call REFERS to (Fix AO).

        The guard exists to stop a worker repeating a call that cannot change
        anything.  Whether a call can change anything depends on the state it
        reads, not only on its own text -- which is why ``file_read`` already
        folded the target file's hash into its fingerprint.  Every other tool
        was fingerprinted on ``(name, arguments)`` alone, so running the SAME
        build command after its script or inputs changed looked identical to
        running it against an unchanged world, and the third attempt was
        blocked.

        Live regression (doc 18, r13b generation 19).  PATCH-R011's final
        frozen build was refused this way::

            worker 自述指向 000e5f8abb30…（79397 bytes）的调用被循环守卫拦截，
            现有 log/json/账本由更早驱动构建产出，最终构建从未被真实执行

        The ledger therefore kept a 20:05:38 computation while the log it
        registered was rewritten at 20:08, and the Verifier correctly rejected
        the node for cross-round inconsistency.  The guard turned "same command,
        different world" into "stuck in a loop".

        Folding referenced content in makes an unchanged repeat still identical
        -- so the guard is not weakened -- while a genuinely new world yields a
        new fingerprint.  Bounded to a few files so the check stays cheap.
        """
        versions: Dict[str, str] = {}
        if self.workspace_root is None:
            return versions
        tokens: list[str] = []
        for value in arguments.values():
            if isinstance(value, str):
                tokens.extend(value.replace("\\", "/").split())
            elif isinstance(value, (list, tuple)):
                tokens.extend(str(item).replace("\\", "/") for item in value)
        for token in tokens:
            if len(versions) >= 4:
                break
            candidate = token.strip("\"'`,;:()[]{}")
            if not candidate or candidate in versions:
                continue
            if "/" not in candidate and not candidate.endswith(
                (".py", ".sh", ".bat", ".sql", ".json", ".js", ".ts")
            ):
                continue
            try:
                path = (self.workspace_root / candidate).resolve()
                if not path.is_relative_to(self.workspace_root) or not path.is_file():
                    continue
                digest = hashlib.sha256()
                with path.open("rb") as source:
                    for chunk in iter(lambda: source.read(65536), b""):
                        digest.update(chunk)
                versions[candidate] = digest.hexdigest()
            except (OSError, ValueError):
                continue
        return versions

    def _content_key(self, call: Dict[str, Any]) -> str | None:
        """Identity of what a ``file_read`` returns: path + content version.

        Independent of the window, so every window of one unchanged file shares
        a key.  ``None`` for anything that is not a workspace file_read.
        """
        function = call.get("function") or {}
        if str(function.get("name", "")) != "file_read" or self.workspace_root is None:
            return None
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                return None
        if not isinstance(arguments, dict) or not arguments.get("path"):
            return None
        try:
            path = (self.workspace_root / str(arguments["path"])).resolve()
            if not path.is_relative_to(self.workspace_root):
                return None
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(65536), b""):
                    digest.update(chunk)
        except (OSError, ValueError):
            return None
        return f"{path}@{digest.hexdigest()}"

    @staticmethod
    def _window(call: Dict[str, Any], result: Dict[str, Any]) -> tuple[int, int]:
        """The byte/char range a read returned, from the result, else the whole file."""
        def _int(source: Dict[str, Any], *names: str) -> int | None:
            for name in names:
                value = source.get(name)
                if isinstance(value, bool):
                    continue
                if isinstance(value, int):
                    return value
                if isinstance(value, str) and value.strip().lstrip("-").isdigit():
                    return int(value.strip())
            return None

        start = _int(result, "offset")
        length = _int(result, "returned_chars")
        if start is None or length is None:
            arguments = (call.get("function") or {}).get("arguments", {})
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (ValueError, TypeError):
                    arguments = {}
            if isinstance(arguments, dict):
                if start is None:
                    start = _int(arguments, "offset") or 0
                if length is None:
                    length = _int(arguments, "max_chars")
        return (int(start or 0), int(length) if length is not None else -1)

    def _state_fingerprint(self, call: Dict[str, Any]) -> str:
        function = call.get("function") or {}
        if self.workspace_root is None:
            return self.fingerprint(call)
        arguments = function.get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                return self.fingerprint(call)
        if not isinstance(arguments, dict):
            return self.fingerprint(call)
        observed: Dict[str, Any] = {}
        if function.get("name") == "file_read" and arguments.get("path"):
            path = (self.workspace_root / str(arguments["path"])).resolve()
            if path.is_relative_to(self.workspace_root):
                try:
                    digest = hashlib.sha256()
                    with path.open("rb") as source:
                        for chunk in iter(lambda: source.read(65536), b""):
                            digest.update(chunk)
                    observed["_observed_content_sha256"] = digest.hexdigest()
                except OSError:
                    observed["_observed_content_sha256"] = "unavailable"
        referenced = self._referenced_file_versions(arguments)
        if referenced:
            observed["_referenced_content_sha256"] = referenced
        if not observed:
            return self.fingerprint(call)
        return self.fingerprint({"function": {
            "name": function.get("name", ""),
            "arguments": {**arguments, **observed},
        }})

    def _stalled_on_content(self, call: Dict[str, Any]) -> bool:
        """True when this file's already-held windows have been re-read past budget."""
        content_key = self._content_key(call)
        if content_key is None:
            return False
        return self._stall_streaks.get(content_key, 0) > self.read_streak_limit

    def admit(self, call: Dict[str, Any]) -> tuple[bool, str]:
        key = self._state_fingerprint(call)
        self._calls[key] += 1
        if self._tool_name(call) == "file_read":
            self._read_keys.add(key)
        if key in self._read_keys:
            if (
                self._failures[key] > self.max_repeats
                or self._read_streaks[key] > self.read_streak_limit
                or self._stalled_on_content(call)
            ):
                return False, "repeated_identical_tool_call"
            return True, key
        if self._calls[key] > self.max_repeats:
            # A required re-verification whose premises changed is not a loop:
            # the same command now runs against different inputs, and the earlier
            # pass is stale by the framework's own execution contract.  Grant the
            # attempt and record why (fix r29, doc 51 §16).
            if self._grant_reverification(call, key, self._calls[key]):
                return True, key
            return False, "repeated_identical_tool_call"
        return True, key

    def _record_premise_baseline(self, key: str, call: Dict[str, Any] | None = None) -> None:
        """Remember the premise versions observed right after an attempt."""
        if self.premise_provider is None:
            return
        try:
            snapshot = self.premise_provider() or {}
        except Exception:
            return
        self._premise_at_last_attempt[key] = {
            str(path): str(value) for path, value in snapshot.items()
        }
        if call is not None:
            call_id = str(call.get("call_id") or call.get("id") or "")
            if call_id:
                self._last_call_id[key] = call_id

    def _grant_reverification(self, call: Dict[str, Any], key: str, repeats: int) -> bool:
        """Admit one re-verification whose premises changed since the last try.

        The comparison is against the versions recorded AFTER the previous
        attempt, so artifacts the command itself writes never count as a change;
        only inputs that moved afterwards (r29: ``summary.json`` and
        ``fix_log.json`` rewritten before the required re-run) open the gate.
        Without a premise provider this never fires.
        """
        if self.premise_provider is None:
            return False
        baseline = self._premise_at_last_attempt.get(key)
        if baseline is None:
            return False
        try:
            current = {
                str(path): str(value)
                for path, value in (self.premise_provider() or {}).items()
            }
        except Exception:
            return False
        changes = changed_premises(baseline, current)
        if not changes:
            return False
        command = command_of_arguments((call.get("function") or {}).get("arguments"))
        self._reverification_log.append({
            "reason": "premises_changed_since_last_execution",
            "tool": self._tool_name(call),
            "call_fingerprint": key,
            "repeat_count": repeats,
            "max_repeats": self.max_repeats,
            "command": command,
            "criterion": self.verification_commands.get(command, ""),
            "declared_verification": command in self.verification_commands,
            "changed_premises": changes,
            "previous_call_id": self._last_call_id.get(key, ""),
            "verification_identity": verification_identity(command or key, current),
        })
        self._reverification_grants[key] += 1
        self._granted_attempt[key] = repeats
        return True

    def take_notices(self) -> List[Dict[str, Any]]:
        """Structured re-verification grants recorded since the last call."""
        notices = list(self._reverification_log)
        self._reverification_log.clear()
        return notices

    def observe(self, call: Dict[str, Any], result: Dict[str, Any]) -> None:
        key = self._state_fingerprint(call)
        status = str(result.get("status", "")).lower()
        if is_guard_blocked(result):
            # Fix r29: our own refusal is not a tool failure.  Counting it fed
            # the failure budget and escalated the decision against the worker
            # for a call the framework itself had just refused.
            self._guard_blocks[key] += 1
            self._record_premise_baseline(key, call)
            return
        if status in {"error", "failed", "failure"}:
            self._failures[key] += 1
        elif status == "success":
            self._failures.pop(key, None)
            name = self._tool_name(call)
            if name == "file_read":
                self._read_streaks[key] += 1
                # Fix AT: same file, same content, a window already returned ->
                # the worker is re-reading what it already holds.  A new window
                # of the same file is forward progress and costs nothing.
                content_key = self._content_key(call)
                if content_key is not None:
                    window = self._window(call, result)
                    seen = self._read_windows.setdefault(content_key, set())
                    if window in seen:
                        self._stall_streaks[content_key] += 1
                    else:
                        seen.add(window)
            elif (name not in self.NON_PROGRESS_TOOLS | self.GENERIC_EXECUTION_TOOLS
                  and not (name == "http_request" and self._readonly_http(call))):
                # Dedicated action/test tools retain their reset semantics.
                # A generic subprocess exit code cannot erase unchanged reads.
                self._read_streaks.clear()
                self._stall_streaks.clear()
                self._read_windows.clear()
        self._record_premise_baseline(key, call)

    @staticmethod
    def _readonly_http(call: Dict[str, Any]) -> bool:
        arguments = (call.get("function") or {}).get("arguments", {})
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (TypeError, ValueError):
                return False
        return (isinstance(arguments, dict)
                and str(arguments.get("method", "GET")).upper() in {"GET", "HEAD", "OPTIONS"})

    def decision(self, call: Dict[str, Any]) -> Dict[str, Any]:
        """Return the next action for a call with a repeated/failing history.

        The guard does not invent a replacement tool.  The tool router or
        Governor remains responsible for selecting one from the registered
        allow-list; this object only emits the bounded recovery decision.
        """
        key = self._state_fingerprint(call)
        repeats = self._calls.get(key, 0)
        failures = self._failures.get(key, 0)
        streak = self._read_streaks.get(key, 0)
        if key in self._read_keys:
            stalled = streak > self.read_streak_limit or self._stalled_on_content(call)
            if failures > self.max_repeats or stalled:
                action = "halt"
                reason = "repeated_identical_tool_call"
            elif failures >= self.failure_threshold:
                action = "switch_tool_or_replan"
                reason = "repeated_tool_failure"
            else:
                action = "retry_or_continue"
                reason = "within_failure_budget"
        else:
            granted = self._granted_attempt.get(key, 0) >= repeats
            if repeats > self.max_repeats and not granted:
                action = "halt"
                reason = "repeated_identical_tool_call"
            elif granted:
                # Fix r29: this attempt was admitted because its premises moved;
                # the repeat count alone must not halt a sanctioned re-run.
                action = "retry_or_continue"
                reason = "reverification_admitted"
            elif failures >= self.failure_threshold:
                action = "switch_tool_or_replan"
                reason = "repeated_tool_failure"
            else:
                action = "retry_or_continue"
                reason = "within_failure_budget"
        return {
            "action": action,
            "reason": reason,
            "call_fingerprint": key,
            "repeat_count": repeats,
            "failure_count": failures,
            "read_streak_count": streak,
            "read_streak_limit": self.read_streak_limit,
            # Fix AT: the same budget counted per (path, content version).  A halt
            # driven by this number must be auditable, otherwise it looks like the
            # per-call streak fired for no reason.
            "content_stall_count": self._stall_streaks.get(self._content_key(call) or "", 0),
        }

    def _decision_for_key(self, key: str, repeats: int) -> str:
        failures = self._failures.get(key, 0)
        if key in self._read_keys:
            if failures > self.max_repeats or self._read_streaks.get(key, 0) > self.read_streak_limit:
                return "halt"
        elif repeats > self.max_repeats and self._granted_attempt.get(key, 0) < repeats:
            return "halt"
        if failures >= self.failure_threshold:
            return "switch_tool_or_replan"
        return "retry_or_continue"

    def snapshot(self) -> Dict[str, Any]:
        return {
            "unique_calls": len(self._calls),
            "repeat_counts": dict(self._calls),
            "failure_counts": dict(self._failures),
            "read_streak_counts": dict(self._read_streaks),
            "max_repeats": self.max_repeats,
            "failure_threshold": self.failure_threshold,
            "read_streak_limit": self.read_streak_limit,
            # Fix r29: a refused call is not a failure, and a refused
            # re-verification is accounted separately from a real loop.
            "guard_block_counts": dict(self._guard_blocks),
            "reverification_grants": dict(self._reverification_grants),
            "decisions": {
                key: self._decision_for_key(key, count)
                for key, count in self._calls.items()
            },
        }

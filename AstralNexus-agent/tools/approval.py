"""Central approval and audit policy for side-effecting tools."""
from __future__ import annotations
from AstralNexus.runtime.run_context import get_env

import json
import os
import threading
from dataclasses import dataclass
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Optional


ApprovalProvider = Callable[[str, dict[str, Any], str, str], str]
_provider: Optional[ApprovalProvider] = None
_provider_lock = threading.RLock()
_session_approved: set[tuple[str, str]] = set()
_always_approved: set[tuple[str, str]] = set()
_providers: dict[str, ApprovalProvider] = {}
_scope: ContextVar[str] = ContextVar("approval_scope", default="")
_bound_provider: ContextVar[tuple[Optional[ApprovalProvider], ...]] = ContextVar("approval_provider", default=())
_thread_state = threading.local()


@dataclass(frozen=True)
class ApprovalDecision:
    status: str
    caller: str
    basis: str
    tool_name: str
    arguments: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "caller": self.caller,
            "basis": self.basis,
            "tool_name": self.tool_name,
            "arguments": self.arguments,
        }


def set_approval_provider(provider: Optional[ApprovalProvider], *, scope: str = "") -> None:
    """Install the foreground approval bridge used by the interactive TUI.

    The model/tool worker calls this boundary synchronously.  The provider is
    expected to block on a UI queue, exactly like AstralNexus' approval worker, and
    return ``once``, ``session``, ``always`` or ``deny``.
    """
    global _provider
    with _provider_lock:
        if scope:
            if provider is None:
                _providers.pop(scope, None)
            else:
                _providers[scope] = provider
        else:
            _provider = provider


def clear_approval_provider(provider: Optional[ApprovalProvider] = None, *, scope: str = "") -> None:
    global _provider
    with _provider_lock:
        if scope:
            if provider is None or _providers.get(scope) is provider:
                _providers.pop(scope, None)
        elif provider is None or _provider is provider:
            _provider = None


def _get_provider(scope: str = "") -> Optional[ApprovalProvider]:
    bound = _bound_provider.get()
    if bound and (not scope or scope == _scope_key()):
        return bound[0]
    with _provider_lock:
        return _providers.get(scope or _scope_key(), _provider)


def _scope_key() -> str:
    explicit = _scope.get()
    if explicit:
        return explicit
    from AstralNexus.tools.context import get_tool_workspace
    root = get_tool_workspace()
    return os.path.normcase(str(root)) if root is not None else "unscoped"


@contextmanager
def approval_scope(scope: str, *, provider: Optional[ApprovalProvider] = None, bind_provider: bool = False):
    """Bind an explicit run/session identity, including on executor threads."""
    token = _scope.set(scope)
    provider_token = _bound_provider.set((provider,)) if bind_provider else None
    try:
        yield
    finally:
        if provider_token is not None:
            _bound_provider.reset(provider_token)
        _scope.reset(token)


def _env_approved(tool_name: str) -> bool:
    explicit = get_env("ASTRALNEXUS_TOOL_APPROVAL", "false").lower() == "true"
    switches = {"file_write": "ASTRALNEXUS_ALLOW_FILE_WRITE", "terminal_exec": "ASTRALNEXUS_ALLOW_TERMINAL"}
    scoped = get_env(switches.get(tool_name, ""), "false").lower() in {"1", "true", "yes"}
    return explicit or scoped


def is_tool_approved(tool_name: str) -> bool:
    """Check the same approval grant from inside a builtin handler."""
    if _env_approved(tool_name):
        return True
    key = (_scope_key(), tool_name)
    with _provider_lock:
        if key in _always_approved or key in _session_approved:
            return True
    return key in getattr(_thread_state, "once", set())


def _grant(tool_name: str, decision: str) -> None:
    key = (_scope_key(), tool_name)
    with _provider_lock:
        if decision == "always":
            _always_approved.add(key)
        elif decision == "session":
            _session_approved.add(key)
        elif decision == "once":
            grants: set[tuple[str, str]] = getattr(_thread_state, "once", set())
            grants.add(key)
            _thread_state.once = grants


def consume_one_shot_grant(tool_name: str) -> None:
    grants: set[tuple[str, str]] = getattr(_thread_state, "once", set())
    grants.discard((_scope_key(), tool_name))
    _thread_state.once = grants


def reset_approval_state() -> None:
    """Test/session cleanup; does not touch environment policy."""
    with _provider_lock:
        _session_approved.clear()
        _always_approved.clear()
        _providers.clear()
    _thread_state.once = set()


def request_approval(tool_name: str, arguments: dict[str, Any], *, caller: str = "unknown",
                     basis: str = "tool_policy") -> ApprovalDecision:
    """Resolve non-interactive approval from the explicit runtime policy.

    Production callers can replace this boundary with a UI/Governor approval
    provider.  Default deny is intentional; a missing approval is never
    interpreted as consent.
    """
    key = (_scope_key(), tool_name)
    with _provider_lock:
        approved = _env_approved(tool_name) or key in _session_approved or key in _always_approved
    if approved:
        status = "approved"
    else:
        provider = _get_provider()
        choice = provider(tool_name, arguments or {}, caller, basis) if provider else "deny"
        aliases = {"1": "once", "approve": "once", "yes": "once", "y": "once",
                   "2": "session", "3": "always", "remember": "always",
                   "4": "deny", "no": "deny", "n": "deny", "cancel": "deny"}
        choice = aliases.get(str(choice).strip().lower(), str(choice).strip().lower())
        if choice in {"once", "session", "always"}:
            _grant(tool_name, choice)
            status = "approved"
        else:
            status = "denied" if choice == "deny" else "approval_pending"
    return ApprovalDecision(
        status=status,
        caller=caller,
        basis=get_env("ASTRALNEXUS_TOOL_APPROVAL_BASIS", basis),
        tool_name=tool_name,
        arguments=json.loads(json.dumps(arguments or {}, ensure_ascii=False)),
    )

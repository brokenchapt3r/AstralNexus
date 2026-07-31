"""Central approval and audit policy for side-effecting tools."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional


ApprovalProvider = Callable[[str, dict[str, Any], str, str], str]
_provider: Optional[ApprovalProvider] = None
_provider_lock = threading.RLock()
_session_approved: set[str] = set()
_always_approved: set[str] = set()
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


def set_approval_provider(provider: Optional[ApprovalProvider]) -> None:
    """Install the foreground approval bridge used by the interactive TUI.

    The model/tool worker calls this boundary synchronously.  The provider is
    expected to block on a UI queue, exactly like AstralNexus' approval worker, and
    return ``once``, ``session``, ``always`` or ``deny``.
    """
    global _provider
    with _provider_lock:
        _provider = provider


def clear_approval_provider(provider: Optional[ApprovalProvider] = None) -> None:
    global _provider
    with _provider_lock:
        if provider is None or _provider is provider:
            _provider = None


def _get_provider() -> Optional[ApprovalProvider]:
    with _provider_lock:
        return _provider


def _env_approved(tool_name: str) -> bool:
    explicit = os.environ.get("ASTRALNEXUS_TOOL_APPROVAL", "false").lower() == "true"
    switches = {"file_write": "ASTRALNEXUS_ALLOW_FILE_WRITE", "terminal_exec": "ASTRALNEXUS_ALLOW_TERMINAL"}
    scoped = os.environ.get(switches.get(tool_name, ""), "false").lower() in {"1", "true", "yes"}
    return explicit or scoped


def is_tool_approved(tool_name: str) -> bool:
    """Check the same approval grant from inside a builtin handler."""
    if _env_approved(tool_name):
        return True
    if tool_name in _always_approved or tool_name in _session_approved:
        return True
    return tool_name in getattr(_thread_state, "once", set())


def _grant(tool_name: str, decision: str) -> None:
    if decision == "always":
        _always_approved.add(tool_name)
    elif decision == "session":
        _session_approved.add(tool_name)
    elif decision == "once":
        grants = getattr(_thread_state, "once", set())
        grants.add(tool_name)
        _thread_state.once = grants


def consume_one_shot_grant(tool_name: str) -> None:
    grants = getattr(_thread_state, "once", set())
    grants.discard(tool_name)
    _thread_state.once = grants


def reset_approval_state() -> None:
    """Test/session cleanup; does not touch environment policy."""
    _session_approved.clear()
    _always_approved.clear()
    _thread_state.once = set()


def request_approval(tool_name: str, arguments: dict[str, Any], *, caller: str = "unknown",
                     basis: str = "tool_policy") -> ApprovalDecision:
    """Resolve non-interactive approval from the explicit runtime policy.

    Production callers can replace this boundary with a UI/Governor approval
    provider.  Default deny is intentional; a missing approval is never
    interpreted as consent.
    """
    approved = _env_approved(tool_name) or tool_name in _session_approved or tool_name in _always_approved
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
        basis=os.environ.get("ASTRALNEXUS_TOOL_APPROVAL_BASIS", basis),
        tool_name=tool_name,
        arguments=json.loads(json.dumps(arguments or {}, ensure_ascii=False)),
    )

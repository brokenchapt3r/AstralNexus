"""Shared account seam: the DeepSeekAccount service shell plus pure helpers.

Port of ``@deepseek-ai/dsh-deepseek-account`` (commit 5badb15). Contract kept:

- ``DeepSeekAccount`` is the abstract account service (upstream cordis Service
  key ``deepseekAccount``); only Host consumers can obtain a request
  credential. The concrete protocol lives in the platform package.
- ``merge_platform_cookies`` merges Cookie header pairs by CASE-SENSITIVE
  name, override wins, unrelated cookies are retained, at most one pair per
  name, malformed pairs (no ``=`` or empty name) are skipped.
- ``desktop_client_headers`` maps the desktop composition's platform to the
  ``x-client-platform`` override (``win32`` → ``desktop-win``, else
  ``desktop-mac``); None leaves non-desktop (web) requests unchanged.
- ``platform_client_headers`` builds the five client identity headers; the
  bundle ID is intentionally empty.
- ``platform_wire_locale`` reduces a UI language to the region-tagged
  Platform locale: leading ``zh`` (split on ``-``/``_``) → ``zh_CN``, else
  ``en_US`` — one normalization shared by headers and request bodies.
- ``is_running_account_task`` identifies running work whose latest bound
  request used the account route (status ``running`` AND
  ``session.request_context()`` provider ``deepseek-account``).

Documented subset (completion-review item): upstream
``installAccountTaskCancellation`` wires two scope events through
``ctx.inject(["agents"])`` — ``agent/error`` (LlmError code
ACCOUNT_SIGN_IN_REQUIRED → emit ``deepseek-account/model-sign-in-required``)
and ``deepseek-account/signed-out`` (cancel every running account task,
``{kind: "hook", reason}`` + ``keepInbox``). The agents scope event surface
arrives with the B4 agent port; this file keeps the two DECISION halves as
independently testable pure functions (``is_account_sign_in_error`` with
structural code discrimination instead of an ``instanceof LlmError``
dependency, and ``cancel_signed_out_account_tasks``) and defers ONLY the
scope wiring. The structural discrimination deliberately does not require
dsh-llm to be ported first — the same technique as the typert protocol port.
"""
from __future__ import annotations

import abc
import re
from typing import Any, Dict, Iterable, Optional

ACCOUNT_SERVICE_KEY = "deepseekAccount"
ACCOUNT_PROVIDER = "deepseek-account"
ACCOUNT_SIGN_IN_REQUIRED = "ACCOUNT_SIGN_IN_REQUIRED"
SIGN_IN_REQUIRED_EVENT = "deepseek-account/model-sign-in-required"
SIGNED_OUT_EVENT = "deepseek-account/signed-out"
SIGN_OUT_CANCEL_REASON = {"kind": "hook", "reason": SIGNED_OUT_EVENT}

_LOCALE_SPLIT = re.compile(r"[-_]")


class DeepSeekAccount(abc.ABC):
    """Account operations; only Host consumers can obtain a request credential.

    Upstream declares no abstract methods on the shell — the concrete surface
    (request credential, sign-in/out lifecycle) is contributed by the platform
    implementation, which registers under the same service key.
    """

    service_name = ACCOUNT_SERVICE_KEY

    def __init__(self, ctx: Any = None):
        self.ctx = ctx


def is_account_sign_in_error(error: Any) -> bool:
    """Structural ACCOUNT_SIGN_IN_REQUIRED discrimination (upstream: ``instanceof LlmError``)."""
    return getattr(error, "code", None) == ACCOUNT_SIGN_IN_REQUIRED


def is_running_account_task(agent: Any) -> bool:
    """Whether account sign-out should interrupt this task.

    Structural against the upstream agent contract (``status`` +
    ``session.requestContext()?.provider``); the native agent surface with a
    request context arrives with the B4 agent port.
    """
    if getattr(agent, "status", None) != "running":
        return False
    session = getattr(agent, "session", None)
    requester = getattr(session, "request_context", None)
    if requester is None:
        requester = getattr(session, "requestContext", None)
    if not callable(requester):
        return False
    context = requester()
    if not isinstance(context, dict):
        return False
    return context.get("provider") == ACCOUNT_PROVIDER


def cancel_signed_out_account_tasks(agents: Iterable[Any]) -> int:
    """Cancel every running account task after a sign-out; returns the count.

    The cancel shape is upstream's verbatim (``{kind: "hook", reason}`` with
    ``keepInbox``) — inbox items survive because the cancellation is a
    provider-state event, not the user retracting the work.
    """
    cancelled = 0
    for agent in agents:
        if is_running_account_task(agent):
            agent.cancel(dict(SIGN_OUT_CANCEL_REASON), {"keepInbox": True})
            cancelled += 1
    return cancelled


def merge_platform_cookies(base: str, override: str) -> str:
    """Merge Cookie header pairs by case-sensitive name; override values win."""
    cookies: Dict[str, str] = {}
    for header in (base, override):
        for pair in header.split(";"):
            separator = pair.find("=")
            if separator < 1:
                continue
            cookies[pair[:separator].strip()] = pair[separator + 1:].strip()
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def desktop_client_headers(platform: Optional[str]) -> Dict[str, str]:
    """Platform request headers shared by account and update-policy clients."""
    if platform is None:
        return {}
    return {"x-client-platform": "desktop-win" if platform == "win32" else "desktop-mac"}


def platform_wire_locale(locale: str) -> str:
    """Reduce a caller's UI language to the region-tagged Platform locale."""
    return "zh_CN" if _LOCALE_SPLIT.split(locale.lower())[0] == "zh" else "en_US"


def platform_client_headers(platform: Optional[str], client: Dict[str, Any]) -> Dict[str, str]:
    """Build the five Platform client identity headers for one call.

    ``client`` carries ``version``, ``locale`` and ``timezone_offset_seconds``
    (native snake_case for the upstream camelCase identity fields).
    """
    headers = {
        "x-client-bundle-id": "",
        "x-client-platform": "web",
    }
    headers.update(desktop_client_headers(platform))
    headers["x-client-version"] = client["version"]
    headers["x-client-locale"] = platform_wire_locale(client["locale"])
    headers["x-client-timezone-offset"] = str(client["timezone_offset_seconds"])
    return headers

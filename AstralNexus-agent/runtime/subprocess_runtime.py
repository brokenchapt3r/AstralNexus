"""The subprocess capability seam contract: the canonical child-environment
scrub, the abstract runtime service, and the control-channel vocabulary.

Port of ``@deepseek-ai/dsh-subprocess`` (commit 5badb15, lib/index.js 103
lines + lib/control.js 27 lines, both read in full). The package is the
Service Definition half of the subprocess seam — the local implementation
lives in ``@deepseek-ai/dsh-subprocess-local`` (B3, not in this wave). What
ships here:

- ``scrubbed_parent_env()``: the ONE scrub definition shared by every
  harness spawner. Credential-shaped names (KEY/PASSWORD/SECRET/TOKEN,
  case-insensitive) are NOT forwarded to children — the harness's own
  DEEPSEEK_API_KEY must never leak into a spawned process implicitly — and
  neither is any ``DSH_*`` name (uppercase comparison: Windows environment
  names are case-insensitive, so a parent ``dsh_*`` entry would otherwise
  survive and read back as ``$env:DSH_*`` in the child). PATH, HOME,
  locale, and proxy variables survive, so child CLIs run normally. The
  resolved proxy environment for children (http_proxy.proxy_environment_for_
  child — the same single answer the parent's transport uses) overlays the
  scrub last: unset slots delete, named slots publish, so a child inherits
  the same routing as its parent. A deliberately supplied credential goes
  through the spawn spec's explicit env layer, which merges AFTER this
  scrub.
- ``SubprocessRuntime``: abstract service (one implementation per context);
  the documented semantics an implementation must honor are carried on the
  class docstring.
- ``SubprocessExecutableNotFoundError``: lookup completed without finding
  an executable file.
- control-channel vocabulary: the inherited byte-channel protocol constants
  shared by subprocess launchers and Node children (fd 7 + the launch
  marker). ``open_inherited_control_channel`` itself is the NODE CHILD side
  of the protocol (it opens an inherited descriptor as a Socket); natively
  the constants ship for launcher/child agreement and the opener is a B3
  sidecar-plane concern — recorded, not faked.

Python adaptations (documented): cordis Service → ABC + SERVICE_KEY;
process.env → os.environ sampled per call (a fresh dict every time, so the
result is safe to hand to a child spawn); the proxy overlay delegates to
the ported http_proxy module (R7), whose child-environment answer already
implements the user-slot/unnamed-slot publication rules verbatim.
"""
from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from AstralNexus.runtime import http_proxy

__all__ = [
    "DSH_ENV_PREFIX", "SENSITIVE_ENV_PATTERN", "scrubbed_parent_env",
    "SubprocessRuntime", "SubprocessExecutableNotFoundError",
    "SUBPROCESS_CONTROL_FD", "SUBPROCESS_CONTROL_ENV",
]

#: Namespace prefix reserved for DeepSeek Harness-managed child environment
#: facts.
DSH_ENV_PREFIX = "DSH_"

#: Credential-shaped environment names are NOT forwarded to children. One
#: heuristic for every in-repo spawner; a deliberately supplied entry
#: survives because explicit env layers merge after the scrub.
SENSITIVE_ENV_PATTERN = re.compile(r"KEY|PASSWORD|SECRET|TOKEN", re.IGNORECASE)

#: Child descriptor reserved for the optional subprocess control channel.
SUBPROCESS_CONTROL_FD = 7

#: Private launch marker consumed before a Node child executes application
#: code.
SUBPROCESS_CONTROL_ENV = "DSH_SUBPROCESS_CONTROL"


def scrubbed_parent_env() -> Dict[str, str]:
    """The ambient parent environment minus credential-shaped names and
    minus all ``DSH_*`` names — the canonical base every harness child
    starts from.

    PATH, HOME, locale, and proxy variables survive, so child CLIs run
    normally; harness identity never leaks implicitly. When a proxy is
    active the result also carries the resolved proxy names, so a child
    inherits the same routing as its parent.

    Returns:
        A fresh environment dict safe to hand to a child spawn.
    """
    env: Dict[str, str] = {}
    for key, value in os.environ.items():
        if (value is not None and SENSITIVE_ENV_PATTERN.search(key) is None
                and not key.upper().startswith(DSH_ENV_PREFIX)):
            env[key] = value
    for name, proxy_value in http_proxy.proxy_environment_for_child().items():
        if proxy_value is None:
            env.pop(name, None)
        else:
            env[name] = proxy_value
    return env


class SubprocessExecutableNotFoundError(RuntimeError):
    """Executable lookup completed without finding an executable file."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message)
        self.name = "SubprocessExecutableNotFoundError"
        if cause is not None:
            self.__cause__ = cause


class SubprocessRuntime(ABC):
    """Abstract subprocess service. Subclass, implement spawn(), and
    register as the host's ``subprocess`` service (one implementation per
    context).

    Implementations must honor these semantics (upstream contract, carried
    verbatim in substance):

    - Executable paths belong to one execution world shared with the
      mounted filesystem provider.
    - spawn() returns a live handle synchronously. Target identity remains
      provider-private; ``done`` resolves with the spawned command's exit
      facts and may reject for spawn or provider failures.
    - Collect-mode readers are offset-based and non-consuming, so
      independent readers never consume one another's output; lossy reads
      report truncation and the spill file holding the complete stream
      when one exists. Piped streams are handed to the caller raw and
      never buffered here.
    - terminate() (and the spec's abort signal) starts the provider's
      documented procedure against its managed range; wait_for_exit()
      observes that same range so a consumer-owned teardown ladder can
      hold each tier on real quiescence. Each provider documents its
      signalling and observability limits.
    - Disposal of the service terminates all still-running managed
      processes and awaits their exit.
    - spawn_terminal() owns terminal allocation, text transport,
      foreground groups, signalling, and whole-session quiescence behind
      one awaited termination method; readiness and persistent-shell
      policy stay in the PTY consumer. Its output stream ends after queued
      terminal output when the top-level process exits.

    Command defaulting, shell semantics, deadlines, protocol framing,
    terminal readiness, and presentation belong to consumers such as the
    bash executor seam — not to this service.
    """

    SERVICE_KEY = "subprocess"

    @abstractmethod
    def spawn(self, spec: Dict[str, Any]) -> Any:
        """Spawn one fully specified process range; returns a live handle."""
        raise NotImplementedError

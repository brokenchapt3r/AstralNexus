"""Process-wide outbound HTTP proxy policy.

Port of ``@deepseek-ai/dsh-http-proxy`` (commit 5badb15, lib/index.js 536
lines read in full). The package has two halves and both are ported:

**policy half (pure, transport-free)** — resolves the launch environment into
one policy and answers which proxy (if any) a URL goes through:
- lowercase-first env reads where a blank value counts as unset (undici's
  ``??`` chain lets an empty lowercase name shadow a populated uppercase
  one — the quirk is kept);
- candidate validation: unparseable → invalid diagnostic; SOCKS schemes →
  their own diagnostic (never silently dropped); other schemes → invalid;
  only ``http``/``https`` are routed;
- scheme resolution: own slot wins; a REJECTED slot keeps that scheme direct
  (diagnostic and route always agree); only an ABSENT slot falls back to
  ``ALL_PROXY`` (and, for https, to the resolved http proxy);
- ``with_loopback`` merges the four loopback entries into every bypass list
  (``*`` short-circuits; both ``::1`` spellings kept);
- ``is_loopback_host`` matches all of 127.0.0.0/8, ``.localhost``, ``::1``,
  ``::``, ``0.0.0.0`` and IPv4-mapped spellings — four literal bypass
  entries alone would still route ``127.0.0.2`` through a proxy that cannot
  reach it;
- ``bypasses_proxy``: entries match a host and every subdomain, a leading
  ``.``/``*.`` is the same thing, ``:port`` narrows, ``*`` bypasses all,
  CIDR is deliberately NOT matched;
- ``proxy_for_url`` is the single answer every consumer consults.

**install half** — owns the process-wide active policy and its publication:
- installing publishes the policy through the proxy environment variables
  (how every consumer that reads an environment — and every spawned child —
  sees the one resolved answer, including the ALL_PROXY fallback and the
  merged loopback bypass);
- the OUTERMOST install owns the inherited snapshot, so a layered install
  never records normalized values as the user's own;
- ``proxy_environment_for_child`` restores what the USER exported (a SOCKS
  proxy curl can use is not swapped for the resolved HTTP one), carries the
  resolved value for schemes the user never named, and always hands down the
  resolved bypass list;
- ``cleared_proxy_env`` removes every proxy name (session replay isolation).

Python adaptations (documented):
- undici's global dispatcher → **the process environment itself**: urllib's
  ``getproxies_environment``/ProxyHandler and every Python HTTP consumer
  resolve proxies from ``os.environ``, so publishing the policy IS installing
  the transport route; there is no dispatcher object to swap. ``installed``
  becomes a boolean marker.
- ``createPolicyDispatcher``'s per-origin routing needs no native analog:
  urllib already consults the environment per request; ``proxy_route_for``
  remains the introspection surface (the ``proxyRouteFor`` answer minus the
  dispatcher field).
- ``NODE_USE_ENV_PROXY`` flag logic is Node-specific (Node fetch ignores env
  proxies without it, and Node exits on an unsupported scheme under it).
  Python children and non-Node children (curl, git) read the variables
  directly, so the native child overlay carries no flag — the "keep the
  user's SOCKS value" behavior needs no guard natively.
- ``URL.parse`` null-on-failure → strict urlparse: empty scheme or a
  ValueError counts as unparseable (urlparse alone is far more permissive
  than WHATWG parsing).
- async install/dispose → synchronous (no dynamic transport import).
- policy field names snake_case (http_proxy/https_proxy/no_proxy/source),
  the host-wide native convention; env variable names keep their upstream
  spellings exactly.
- win32 note: the launch-environment snapshot indexes case-insensitively on
  Windows, so the lowercase-first precedence collapses to whichever spelling
  the layer holds — the same collapse the OS environment itself performs.
"""
from __future__ import annotations

import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

__all__ = [
    "LOOPBACK_NO_PROXY", "POLICY_ENV_NAMES", "PROXY_ENV_NAMES", "SUPPORTED_PROTOCOLS",
    "SOCKS_PROTOCOLS", "DIRECT_POLICY", "read_env", "accept_proxy_url",
    "is_supported_proxy_url", "resolve_scheme", "with_loopback", "split_host_port",
    "is_loopback_host", "bypasses_proxy", "resolve_proxy_policy", "proxy_for_url",
    "proxy_route_for", "snapshot_proxy_env", "write_proxy_env", "apply_policy_env",
    "install_global_proxy", "proxy_environment_for_child", "install_proxy_from_environment",
    "cleared_proxy_env", "active_policy",
]

LOOPBACK_NO_PROXY = ("localhost", "127.0.0.1", "::1", "[::1]")

POLICY_ENV_NAMES: Dict[str, Tuple[str, str]] = {
    "http_proxy": ("http_proxy", "HTTP_PROXY"),
    "https_proxy": ("https_proxy", "HTTPS_PROXY"),
    "no_proxy": ("no_proxy", "NO_PROXY"),
}

PROXY_ENV_NAMES: Tuple[str, ...] = tuple(
    name for names in POLICY_ENV_NAMES.values() for name in names
) + ("all_proxy", "ALL_PROXY")

SUPPORTED_PROTOCOLS = ("http", "https")
SOCKS_PROTOCOLS = ("socks", "socks4", "socks4a", "socks5", "socks5h")

DIRECT_POLICY: Dict[str, Any] = {"no_proxy": "", "source": "none"}


# ------------------------------------------------------------ policy half


def read_env(env: Any, lower: str) -> Optional[Dict[str, str]]:
    """One name in lowercase-first order; a blank value counts as unset."""
    for name in (lower, lower.upper()):
        resolution = env.get(name)
        if resolution is None:
            continue
        value = resolution.value.strip()
        if value != "":
            return {"value": value, "name": name}
    return None


def _parse_proxy_url(value: str) -> Optional[Any]:
    """The strict URL.parse equivalent: None when WHATWG parsing would fail."""
    try:
        parsed = urlparse(value)
    except ValueError:
        return None
    if not parsed.scheme:
        return None  # no scheme: URL.parse returns null for such relative input
    return parsed


def accept_proxy_url(candidate: Optional[Dict[str, str]],
                     diagnostics: List[Dict[str, str]]) -> Dict[str, Any]:
    """Validate one candidate; distinguish absent / rejected / accepted."""
    if candidate is None:
        return {"kind": "absent"}
    parsed = _parse_proxy_url(candidate["value"])
    if parsed is None:
        diagnostics.append({
            "kind": "invalid", "origin": candidate["name"],
            "message": f"{candidate['name']} is not a valid URL; connecting directly"})
        return {"kind": "rejected"}
    if parsed.scheme in SOCKS_PROTOCOLS:
        diagnostics.append({
            "kind": "socks", "origin": candidate["name"],
            "message": f"{candidate['name']} names a SOCKS proxy, which is not "
                       f"supported; connecting directly for that scheme — set an "
                       f"http:// or https:// proxy URL instead"})
        return {"kind": "rejected"}
    if parsed.scheme not in SUPPORTED_PROTOCOLS:
        diagnostics.append({
            "kind": "invalid", "origin": candidate["name"],
            "message": f"{candidate['name']} uses the unsupported {parsed.scheme}// "
                       f"scheme; connecting directly for that scheme — set an "
                       f"http:// or https:// proxy URL instead"})
        return {"kind": "rejected"}
    return {"kind": "accepted", "value": candidate["value"]}


def is_supported_proxy_url(value: str) -> bool:
    parsed = _parse_proxy_url(value)
    return parsed is not None and parsed.scheme in SUPPORTED_PROTOCOLS


def resolve_scheme(own: Dict[str, Any], *fallbacks: Optional[str]) -> Optional[str]:
    """Own slot wins; rejected keeps direct; only absent takes a fallback."""
    if own["kind"] == "accepted":
        return own["value"]
    if own["kind"] == "rejected":
        return None
    for value in fallbacks:
        if value is not None:
            return value
    return None


def with_loopback(no_proxy: Optional[str]) -> str:
    """Merge the loopback entries in, preserving the caller's entries and order."""
    entries = [entry.strip() for entry in re.split(r"[,\s]+", no_proxy or "")]
    entries = [entry for entry in entries if entry != ""]
    if "*" in entries:
        return "*"
    present = {entry.lower() for entry in entries}
    merged = list(entries)
    merged.extend(entry for entry in LOOPBACK_NO_PROXY if entry not in present)
    return ",".join(merged)


def split_host_port(entry: str) -> Dict[str, Optional[str]]:
    """One bypass entry into host and optional port (IPv6-literal safe)."""
    if entry.startswith("["):
        close = entry.find("]")
        if close != -1:
            rest = entry[close + 1:]
            host = entry[1:close]
            if rest.startswith(":"):
                return {"host": host, "port": rest[1:]}
            return {"host": host, "port": None}
    colon = entry.find(":")
    if colon != -1 and entry.find(":", colon + 1) == -1:
        return {"host": entry[:colon], "port": entry[colon + 1:]}
    return {"host": entry, "port": None}


_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
LOOPBACK_IPV4 = re.compile(rf"^127\.{_OCTET}\.{_OCTET}\.{_OCTET}$")
_MAPPED_HIGH = re.compile(r"^::ffff:([0-9a-f]{1,4}):[0-9a-f]{1,4}$")


def is_loopback_host(hostname: str) -> bool:
    """Whether a host names this machine (all of 127/8, .localhost, ::1, mapped)."""
    host = re.sub(r"^\[|\]$", "", hostname).rstrip(".").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    if host in ("::1", "::", "0.0.0.0"):
        return True
    mapped = _MAPPED_HIGH.match(host)
    if mapped is not None:
        return int(mapped.group(1), 16) >> 8 == 127
    if host.startswith("::ffff:"):
        host = host[7:]
    return bool(LOOPBACK_IPV4.match(host))


def bypasses_proxy(no_proxy: str, url: Any) -> bool:
    """Whether the effective bypass list exempts one parsed URL."""
    host = re.sub(r"^\[|\]$", "", url.hostname or "").rstrip(".").lower()
    port = url.port if url.port else ("443" if url.scheme == "https" else "80")
    port = str(port)
    for raw in re.split(r"[,\s]+", no_proxy):
        entry = raw.strip().lower()
        if entry == "":
            continue
        if entry == "*":
            return True
        split = split_host_port(entry)
        if split["port"] is not None and split["port"] != port:
            continue
        candidate = re.sub(r"^\*?\.", "", split["host"] or "").rstrip(".")
        if candidate == "":
            continue
        if host == candidate or host.endswith(f".{candidate}"):
            return True
    return False


def resolve_proxy_policy(env: Any) -> Dict[str, Any]:
    """The policy to install plus every rejected candidate, in considered order."""
    diagnostics: List[Dict[str, str]] = []
    all_candidate = accept_proxy_url(read_env(env, "all_proxy"), diagnostics)
    all_value = all_candidate["value"] if all_candidate["kind"] == "accepted" else None
    env_http = accept_proxy_url(read_env(env, "http_proxy"), diagnostics)
    env_https = accept_proxy_url(read_env(env, "https_proxy"), diagnostics)
    http_proxy = resolve_scheme(env_http, all_value)
    https_proxy = resolve_scheme(env_https, all_value, http_proxy)
    if http_proxy is None and https_proxy is None:
        return {"policy": dict(DIRECT_POLICY), "diagnostics": diagnostics}
    policy: Dict[str, Any] = {"source": "env"}
    if http_proxy is not None:
        policy["http_proxy"] = http_proxy
    if https_proxy is not None:
        policy["https_proxy"] = https_proxy
    no_proxy_resolution = read_env(env, "no_proxy")
    policy["no_proxy"] = with_loopback(no_proxy_resolution["value"]
                                       if no_proxy_resolution else None)
    return {"policy": policy, "diagnostics": diagnostics}


def proxy_for_url(policy: Dict[str, Any], url: Any) -> Optional[str]:
    """The single routing answer both the transport and consumers consult."""
    if url.scheme == "https":
        proxy = policy.get("https_proxy")
    elif url.scheme == "http":
        proxy = policy.get("http_proxy")
    else:
        proxy = None
    if proxy is None:
        return None
    if is_loopback_host(url.hostname or ""):
        return None
    return None if bypasses_proxy(policy.get("no_proxy", ""), url) else proxy


# ----------------------------------------------------------- install half

_LOCK = threading.RLock()
_active: Optional[Dict[str, Any]] = None
_inherited_proxy_env: Optional[Dict[str, Optional[str]]] = None
_installed = False

DIRECT_ROUTE: Dict[str, Any] = {"proxied": False}


def active_policy() -> Optional[Dict[str, Any]]:
    """The active policy, or None until one is installed (introspection)."""
    with _LOCK:
        return dict(_active) if _active is not None else None


def proxy_route_for(url: Any) -> Dict[str, Any]:
    """Decide how to send one request (the dispatcher-free native answer)."""
    with _LOCK:
        policy = _active
        installed = _installed
    if policy is None or not installed:
        return dict(DIRECT_ROUTE)
    proxy = proxy_for_url(policy, url)
    if proxy is None:
        return dict(DIRECT_ROUTE)
    return {"proxied": True, "proxy": proxy}


def snapshot_proxy_env() -> Dict[str, Optional[str]]:
    """Every published name as os.environ holds it now (None marks absent)."""
    snapshot: Dict[str, Optional[str]] = {}
    for names in POLICY_ENV_NAMES.values():
        for name in names:
            snapshot[name] = os.environ.get(name)
    return snapshot


def write_proxy_env(values: Dict[str, Optional[str]]) -> Callable[[], None]:
    """Set every published name; None removes; returns the exact restore."""
    previous = snapshot_proxy_env()
    for name in previous:
        value = values.get(name)
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value

    def restore() -> None:
        for name, old in previous.items():
            if old is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = old

    return restore


def apply_policy_env(policy: Dict[str, Any]) -> Callable[[], None]:
    """Publish a policy through the environment; the OUTERMOST install owns
    the inherited snapshot a layered install would otherwise normalize."""
    global _inherited_proxy_env
    with _LOCK:
        previous_inherited = _inherited_proxy_env
        if _inherited_proxy_env is None:
            _inherited_proxy_env = snapshot_proxy_env()
    published: Dict[str, Optional[str]] = {}
    for field, names in POLICY_ENV_NAMES.items():
        value = policy.get(field)
        for name in names:
            published[name] = value
    restore = write_proxy_env(published)

    def restore_all() -> None:
        global _inherited_proxy_env
        restore()
        with _LOCK:
            _inherited_proxy_env = previous_inherited

    return restore_all


def install_global_proxy(policy: Dict[str, Any]) -> Callable[[], None]:
    """Route this process's outbound HTTP through the policy.

    Natively, publishing the policy environment IS installing the transport
    route (urllib and every spawned child resolve proxies from it). A policy
    that proxies nothing clears the published names and leaves the user's
    inherited values for restoration. The disposer restores the previous
    policy, publication state, and environment.
    """
    global _active, _installed, _inherited_proxy_env
    with _LOCK:
        previous_policy = _active
        previous_installed = _installed
        if policy.get("source") == "none":
            if previous_policy is None:
                _active = dict(policy)
                _installed = False

                def restore_first() -> None:
                    global _active, _installed
                    with _LOCK:
                        _active = previous_policy
                        _installed = previous_installed

                return restore_first
            restore_env: Optional[Callable[[], None]] = None
            if _inherited_proxy_env is not None:
                restore_env = write_proxy_env(_inherited_proxy_env)
            _active = dict(policy)
            _installed = False

            def restore_direct() -> None:
                global _active, _installed
                with _LOCK:
                    _active = previous_policy
                    _installed = previous_installed
                if restore_env is not None:
                    restore_env()

            return restore_direct
        restore_published = apply_policy_env(policy)
        _active = dict(policy)
        _installed = True

    def restore_proxied() -> None:
        global _active, _installed
        with _LOCK:
            _active = previous_policy
            _installed = previous_installed
        restore_published()

    return restore_proxied


def proxy_environment_for_child() -> Dict[str, Optional[str]]:
    """The proxy environment a spawned child needs.

    Each name the USER exported keeps the value they wrote (a SOCKS proxy
    curl uses is not swapped for the resolved HTTP one); a scheme the user
    named in neither casing carries the resolved value so the child's routing
    never silently diverges; the bypass list is always the resolved one. An
    empty overlay means no proxy is active.
    """
    with _LOCK:
        policy = _active
        inherited = dict(_inherited_proxy_env) if _inherited_proxy_env is not None \
            else None
    if policy is None or policy.get("source") == "none" or inherited is None:
        return {}
    overlay: Dict[str, Optional[str]] = {}
    for field, names in POLICY_ENV_NAMES.items():
        resolved = policy.get(field)
        named = field != "no_proxy" and any(inherited.get(name) is not None
                                            for name in names)
        for name in names:
            overlay[name] = inherited.get(name) if named else resolved
    return overlay


def install_proxy_from_environment(env: Any,
                                   report: Callable[[str], None]) -> Callable[[], None]:
    """Resolve, report, and install as one operation (the launcher sequence).

    A value the environment supplies but this package cannot use is reported
    and skipped rather than thrown: the variable may have been exported for
    another tool, and a proxy the harness cannot use must not stop the agent
    from starting.
    """
    resolved = resolve_proxy_policy(env)
    for diagnostic in resolved["diagnostics"]:
        report(diagnostic["message"])
    return install_global_proxy(resolved["policy"])


def cleared_proxy_env() -> Dict[str, Optional[str]]:
    """The overlay that removes every proxy name from a spawned child."""
    return {name: None for name in PROXY_ENV_NAMES}

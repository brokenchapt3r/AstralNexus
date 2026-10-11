"""File-backed credentials provider over ``$DSH_HOME/.credentials.yaml``.

Port of ``@deepseek-ai/dsh-credentials-local`` (commit 5badb15). Contract kept:
- Layering by trust: inherited process environment (read-only, WINS) >
  ``$DSH_HOME/.credentials.yaml`` (provider-managed, writable) >
  ``<invocation cwd>/.env`` (project-env fallback) > ``$DSH_HOME/.env``
  (user-env fallback). The inherited environment is this run's explicit
  intent and is *visibly* read-only: a shadowed ``set``/``unset`` fails loud
  instead of silently no-effecting.
- The document is a strict versioned mapping (``version: 1`` + ``refs`` +
  ``records``); everything else is rejected rather than skipped — an
  unversioned root (with pre-release flat-layout migration guidance), an
  unknown top-level key, a non-addressable ref/key, a wrong-typed or empty
  value, an unknown record tag or field, duplicate keys. Parse diagnostics
  never quote the source line, because it holds a secret.
- The recognized pre-release flat layout is migrated in place under the writer
  lock (values carried verbatim, original lines nested under ``refs:``); any
  document the recognizer declines keeps the loud rejection — a document this
  build cannot prove it understands is never rewritten.
- Writes re-read the document under the cross-process writer lock
  (30s wait — sized by a token refresh's network round trip) before patching
  ONLY their own key; comments and the formatting of every untouched entry
  survive (round-trip editing). Files are written atomically at 0600 in a
  0700 directory; POSIX owner-only permission is asserted before any read
  (skipped on Windows, never faked).
- External edits hot-publish through the seam: unchanged content (including
  this provider's own writes) is a no-op; each reload replaces the snapshot
  wholesale so a deleted entry never lingers; an unreadable/invalid document
  keeps the last good snapshot and warns — hot reload never takes the process
  down. An absent file publishes the empty store.
- Grant payloads must survive a JSON round trip in BOTH directions (non-finite
  numbers, cycles, dates, class instances rejected; no value ever quoted in a
  diagnostic). api-key records are validated where they are written, so a
  mutation can never persist a document the next boot would reject.
- One exclusive operation chain: watcher reloads and edits run one at a time,
  so an edit can never render from text a concurrent reload is replacing.
  Disposal refuses new writes and lets in-flight work finish.

Python adaptations (documented):
- ``yaml`` (npm) round-trip Document editing becomes ``ruamel.yaml`` rt mode.
  Comment attachment differs: the comment above a section's FIRST entry sits
  on the section map, the comment above entry N>1 sits on entry N-1's
  post-slot. ``_delete_section_entry`` reproduces the upstream rule — the
  deleted entry's own annotation goes with it, every other entry's comment is
  preserved and re-hosted when its carrier is deleted.
- The promise operation chain becomes a process-local ``RLock`` (FIFO order is
  not promised; the cross-process file lock remains the authoritative
  serialization point), and the async seam methods become synchronous.
- chokidar ``awaitWriteFinish`` becomes a watchdog Observer plus a
  coalescing debounce timer; torn reads are additionally absorbed by the
  keep-last-good reload policy.
- Zod config schema becomes explicit validation with the same defaults
  (watch=True, debounceMs=100 ≥ 0); the plugin manifest's JSON Schema mirrors
  it for host-side config validation.
- YAML parse diagnostics carry the ruamel error class name + line/column
  (upstream reports the yaml-lib error code); neither quotes content.
- Recursive anchors: upstream npm-yaml rejects an excessive alias depth at
  parse time; ruamel degrades a recursive alias to a null placeholder, which
  is JSON-lossless and admitted. The cycle guard still rejects in-memory
  cyclic payloads at the write boundary (``assert_json_value``).
"""
from __future__ import annotations

import copy
import io
import logging
import math
import os
import sys
import threading
from typing import Any, Callable, Dict, List, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap
from ruamel.yaml.error import MarkedYAMLError

from ..dshutil.atomic_write import with_file_lock, write_file_atomic
from ..dshutil.home_paths import canonicalize_watch_path, resolve_dsh_home
from ..dshutil.launch_environment import launch_environment_of
from ..dshutil.values import deep_equal_json
from .base import CredentialProvider, credential_ref, parse_credential_key

CREDENTIALS_FILENAME = ".credentials.yaml"
DOCUMENT_VERSION = 1
# Permission bits outside the owner; a credentials document must have none.
GROUP_OTHER_BITS = 0o77
# A record mutation may hold the lock across a network round trip (token
# refresh); every writer of this document waits this long, like upstream.
DOCUMENT_LOCK_WAIT_MS = 30000
_TOP_LEVEL_KEYS = ("version", "refs", "records")


class CredentialsDocumentError(RuntimeError):
    """A credentials document that exists but cannot be trusted (never 'empty')."""


def _yaml() -> YAML:
    instance = YAML()  # round-trip mode: comments and formatting survive
    instance.allow_duplicate_keys = False
    setattr(instance, "width", 4096)  # secrets are never line-wrapped (stub gap)
    return instance


def resolve_spec(config: Dict[str, Any]) -> Dict[str, Any]:
    """Explicit ``path`` wins; otherwise ``<harness home>/.credentials.yaml``."""
    configured = config.get("path")
    if configured is None:
        filename = os.path.join(resolve_dsh_home(config.get("dshHome")), CREDENTIALS_FILENAME)
    else:
        filename = str(configured)
    watch = config.get("watch")
    debounce = config.get("debounceMs")
    return {
        "filename": os.path.abspath(filename),
        "watch": True if watch is None else bool(watch),
        "debounceMs": 100 if debounce is None else int(debounce),
    }


def validate_config(config: Any) -> Dict[str, Any]:
    """Zod-equivalent admission of the plugin config (same fields and defaults)."""
    if config is None:
        config = {}
    if not isinstance(config, dict):
        raise CredentialsDocumentError("credentials-local: config must be a mapping")
    for field in ("path", "dshHome"):
        value = config.get(field)
        if value is not None and not isinstance(value, str):
            raise CredentialsDocumentError(f"credentials-local: config.{field} must be a string")
    watch = config.get("watch")
    if watch is not None and not isinstance(watch, bool):
        raise CredentialsDocumentError("credentials-local: config.watch must be a boolean")
    debounce = config.get("debounceMs")
    if debounce is not None:
        if isinstance(debounce, bool) or not isinstance(debounce, (int, float)) or debounce < 0:
            raise CredentialsDocumentError("credentials-local: config.debounceMs must be a number >= 0")
    return dict(config)


def _is_absent(error: OSError) -> bool:
    import errno
    return error.errno == errno.ENOENT


def assert_owner_only(filename: str) -> None:
    """Reject a document other OS users can read, BEFORE its contents are read.

    POSIX only: Windows has no mode to inspect — its ACLs are not expressible
    here — so the check is skipped rather than faked.
    """
    try:
        mode = os.stat(filename).st_mode
    except OSError as error:
        if not _is_absent(error):
            raise
        canonicalize_watch_path(filename)  # proves the path hierarchy is valid
        return
    if sys.platform == "win32":
        return
    if mode & GROUP_OTHER_BITS == 0:
        return
    raise CredentialsDocumentError(
        f'credentials-local: {filename} is readable beyond its owner '
        f'(mode {oct(mode & 0o777)[2:]}); run "chmod 600 {filename}" before starting again')


def describe_yaml_error(error: Any) -> str:
    """One YAML parse failure WITHOUT quoting the source (the line holds a secret)."""
    mark = getattr(error, "problem_mark", None)
    where = "" if mark is None else f" at line {mark.line + 1}, column {mark.column + 1}"
    code = type(error).__name__
    return f"{code}{where}"


def parse_credentials_document(text: str, filename: str) -> Dict[str, Dict[str, Any]]:
    """Parse one credentials document; everything invalid is rejected, not skipped."""
    try:
        root = _yaml().load(text)
    except MarkedYAMLError as error:
        raise CredentialsDocumentError(
            f"credentials-local: invalid document at {filename}: {describe_yaml_error(error)}") from None
    if root is None:
        root = {}
    if not isinstance(root, dict) or isinstance(root, list):
        raise TypeError(f"credentials-local: {filename} must be a mapping")
    keys = list(root.keys())
    if len(keys) == 0:
        return {"refs": {}, "records": {}}
    if "version" not in root:
        plural = "entry" if len(keys) == 1 else "entries"
        raise CredentialsDocumentError(
            f"credentials-local: {filename} uses the pre-release flat layout. Add `version: 1` "
            f"and nest the existing {len(keys)} {plural} under `refs:`. No values need to change.")
    if root["version"] != DOCUMENT_VERSION:
        import json
        raise CredentialsDocumentError(
            f"credentials-local: {filename} declares version {json.dumps(root['version'])}; "
            f"this build reads version {DOCUMENT_VERSION}")
    for key in keys:
        if key not in _TOP_LEVEL_KEYS:
            raise CredentialsDocumentError(
                f'credentials-local: unknown top-level key "{key}" in {filename}')
    return {"refs": _parse_refs(root.get("refs"), filename),
            "records": _parse_records(root.get("records"), filename)}


def render_flat_layout_migration(text: str) -> Optional[str]:
    """Version-1 layout for a RECOGNIZED pre-release flat document, else None.

    Recognized exactly: a non-empty top-level mapping of addressable reference
    names to non-empty string scalars, no ``version`` key, no directives. The
    rewrite nests the original lines verbatim under ``refs:`` at two spaces,
    so comments, blank lines, and each value's spelling survive byte for byte.
    Anything declined keeps the loud rejection — never rewrite a document this
    build cannot prove it understands.
    """
    try:
        root = _yaml().load(text)
    except MarkedYAMLError:
        return None
    if not isinstance(root, dict) or isinstance(root, list) or len(root) == 0:
        return None
    for line in text.split("\n"):
        if line.startswith(("%", "---", "...")):
            return None
    for key, value in root.items():
        if not isinstance(key, str) or key == "version":
            return None
        try:
            credential_ref(key)
        except TypeError:
            return None
        if not isinstance(value, str) or len(value) == 0:
            return None
    indented = "\n".join(line if len(line) == 0 else f"  {line}" for line in text.split("\n"))
    suffix = "" if text.endswith("\n") else "\n"
    return f"version: 1\nrefs:\n{indented}{suffix}"


def _as_section(section: Any, name: str, filename: str) -> Dict[str, Any]:
    """One section as a plain mapping; absent and null both mean empty."""
    if section is None:
        return {}
    if not isinstance(section, dict) or isinstance(section, list):
        raise TypeError(f'credentials-local: "{name}" in {filename} must be a mapping')
    return dict(section)


def _parse_refs(section: Any, filename: str) -> Dict[str, str]:
    """Admit a ``refs`` section: POSIX-identifier keys over non-empty strings."""
    entries: Dict[str, str] = {}
    for key, value in _as_section(section, "refs", filename).items():
        credential_ref(key)
        if not isinstance(value, str):
            raise TypeError(f'credentials-local: the value for "{key}" in {filename} must be a string')
        if len(value) == 0:
            raise CredentialsDocumentError(
                f'credentials-local: the value for "{key}" in {filename} is empty; remove the key instead')
        entries[key] = value
    return entries


def _parse_records(section: Any, filename: str) -> Dict[str, Dict[str, Any]]:
    """Admit a ``records`` section: ``<scope>/<id>`` keys over tagged records."""
    entries: Dict[str, Dict[str, Any]] = {}
    for key, value in _as_section(section, "records", filename).items():
        parse_credential_key(key)
        entries[key] = _parse_record(key, value, filename)
    return entries


def _assert_fields(key: str, fields: Dict[str, Any], allowed: List[str], filename: str) -> None:
    """Reject a field the tag does not define, so a typo is not silently dropped."""
    for field in fields:
        if field not in allowed:
            raise CredentialsDocumentError(
                f'credentials-local: record "{key}" in {filename} has unknown field "{field}"')


def _parse_record(key: str, value: Any, filename: str) -> Dict[str, Any]:
    if not isinstance(value, dict) or isinstance(value, list):
        raise TypeError(f'credentials-local: record "{key}" in {filename} must be a mapping')
    kind = value.get("kind")
    if kind == "api-key":
        _assert_fields(key, value, ["kind", "key", "env"], filename)
        api_key = value.get("key")
        if api_key is not None and (not isinstance(api_key, str) or len(api_key) == 0):
            raise TypeError(
                f'credentials-local: record "{key}" in {filename} has a non-string or empty key')
        record: Dict[str, Any] = {"kind": "api-key"}
        if api_key is not None:
            record["key"] = api_key
        env = _parse_record_env(key, value.get("env"), filename)
        if env is not None:
            record["env"] = env
        return record
    if kind == "grant":
        _assert_fields(key, value, ["kind", "payload"], filename)
        if "payload" not in value:
            raise CredentialsDocumentError(
                f'credentials-local: record "{key}" in {filename} has no payload')
        assert_json_value(f'record "{key}" payload in {filename}', value["payload"], set())
        return {"kind": "grant", "payload": value["payload"]}
    if kind is None:
        raise CredentialsDocumentError(f'credentials-local: record "{key}" in {filename} has no kind')
    import json
    raise CredentialsDocumentError(
        f'credentials-local: record "{key}" in {filename} has unknown kind {json.dumps(kind)}')


def _parse_record_env(key: str, env: Any, filename: str) -> Optional[Dict[str, str]]:
    """Admit an api-key record's provider environment: POSIX names over non-empty strings."""
    if env is None:
        return None
    if not isinstance(env, dict) or isinstance(env, list):
        raise TypeError(f'credentials-local: record "{key}" in {filename} has a non-mapping env')
    parsed: Dict[str, str] = {}
    for name, value in env.items():
        credential_ref(name)
        if not isinstance(value, str) or len(value) == 0:
            raise TypeError(
                f'credentials-local: record "{key}" env "{name}" in {filename} must be a non-empty string')
        parsed[name] = value
    return parsed


def assert_storable_api_key(key: str, record: Dict[str, Any]) -> None:
    """Refuse an api-key record the read path could not admit, BEFORE it is rendered.

    A durable-boundary write is validated where it is written, so a mutation
    can never persist a document the next boot would reject.
    """
    api_key = record.get("key")
    if api_key is not None and len(api_key) == 0:
        raise TypeError(
            f'credentials-local: record "{key}" has an empty key; omit the field instead')
    for name, value in (record.get("env") or {}).items():
        credential_ref(name)
        if not isinstance(value, str) or len(value) == 0:
            raise TypeError(
                f'credentials-local: record "{key}" env "{name}" must be a non-empty string')


def assert_json_value(where: str, value: Any, seen: set) -> None:
    """Reject a payload that cannot survive a JSON round trip; never quote the value."""
    if value is None or isinstance(value, (str, bool)):
        return
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise TypeError(f"credentials-local: {where} holds a non-finite number")
        return
    if isinstance(value, (dict, list, tuple)):
        if id(value) in seen:
            raise TypeError(f"credentials-local: {where} is cyclic")
        seen.add(id(value))
        for nested in value.values() if isinstance(value, dict) else value:
            assert_json_value(where, nested, seen)
        seen.discard(id(value))
        return
    raise TypeError(f"credentials-local: {where} holds a value JSON cannot represent")


def _mutable_document(text: Optional[str]) -> Any:
    """The comment-preserving tree one edit renders from, version-stamped."""
    document = _yaml().load(text) if text is not None else CommentedMap()
    if document is None:
        document = CommentedMap()
    document["version"] = DOCUMENT_VERSION
    return document


def _dump(document: Any) -> str:
    stream = io.StringIO()
    _yaml().dump(document, stream)
    return stream.getvalue()


def render_ref(text: Optional[str], ref: str, value: Optional[str]) -> str:
    """Next document text with one reference set or deleted."""
    document = _mutable_document(text)
    if value is None:
        _delete_section_entry(document, "refs", ref)
    else:
        section = _ensure_section(document, "refs")
        section[ref] = value
    return _dump(document)


def render_record(text: Optional[str], key: str, record: Optional[Dict[str, Any]]) -> str:
    """Next document text with one record written or deleted.

    The record node is replaced wholesale rather than edited field by field:
    records are machine-written, so there is no hand formatting to preserve.
    """
    document = _mutable_document(text)
    if record is None:
        _delete_section_entry(document, "records", key)
    else:
        section = _ensure_section(document, "records")
        section[key] = dict(record)
    return _dump(document)


def _ensure_section(document: Any, section: str) -> Any:
    existing = document.get(section)
    if not isinstance(existing, CommentedMap):
        existing = CommentedMap()
        document[section] = existing
    return existing


def _delete_section_entry(document: Any, section: str, key: str) -> None:
    """Remove one entry, taking ITS annotation with it and preserving every other.

    ruamel hosts the comment above a section's first entry TWICE (on the
    section map's ``ca.comment`` and in the parent's post-slot for the
    section), and the comment above entry N>1 on entry N-1's post-slot.
    Upstream rule: a comment block written above the deleted entry goes with
    it (never moves onto whichever entry becomes first — that reads as an
    annotation of a credential nobody wrote it for); the comment above the
    NEXT entry is re-hosted so it survives its carrier's deletion. An emptied
    section is removed wholesale: ruamel renders an empty commented section
    into malformed text, and an absent section parses as the empty store.
    """
    existing = document.get(section)
    if not isinstance(existing, CommentedMap) or key not in existing:
        return
    keys = list(existing.keys())
    index = keys.index(key)
    items = existing.ca.items
    entry_ca = items.get(key)
    carried = entry_ca[2] if entry_ca is not None and len(entry_ca) > 2 else None
    top_ca = document.ca.items.get(section)
    if index == 0:
        existing.ca.comment = None  # first entry's own annotation goes with it
        if top_ca is not None and len(top_ca) > 3:
            top_ca[3] = None  # its duplicated host on the parent slot, too
    else:
        previous_ca = items.get(keys[index - 1])
        if previous_ca is not None and len(previous_ca) > 2:
            previous_ca[2] = None  # key's own annotation (hosted on previous)
    del existing[key]
    items.pop(key, None)
    if len(existing) == 0:
        document.ca.items.pop(section, None)
        del document[section]
        return
    if carried is None:
        return
    remaining = list(existing.keys())
    if index == 0:
        # Re-host the next entry's comment on the parent slot, which is the
        # host ruamel actually renders for a nested block section.
        slot = document.ca.items.setdefault(section, [None, None, None, None])
        slot[3] = [carried]
    elif index <= len(remaining):
        host = remaining[index - 1]
        host_ca = items.setdefault(host, [None, None, None, None])
        if host_ca[2] is None:
            host_ca[2] = carried


def _changed_refs(previous: Dict[str, str], current: Dict[str, str]) -> List[str]:
    changed = []
    for key in dict.fromkeys(list(previous.keys()) + list(current.keys())):
        if previous.get(key) == current.get(key):
            continue
        changed.append(credential_ref(key))
    return changed


def _changed_records(previous: Dict[str, Any], current: Dict[str, Any]) -> List[str]:
    changed = []
    for key in dict.fromkeys(list(previous.keys()) + list(current.keys())):
        if deep_equal_json(previous.get(key), current.get(key)):
            continue
        changed.append(parse_credential_key(key))
    return changed


class LocalCredentialProvider(CredentialProvider):
    """File-backed credentials provider (``$DSH_HOME/.credentials.yaml``)."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, ctx: Any = None,
                 logger: Optional[logging.Logger] = None,
                 launch_environment: Any = None):
        super().__init__(ctx, logger or logging.getLogger("astralnexus.credentials.local"))
        self.config = validate_config(config)
        self.spec = resolve_spec(self.config)
        # ``launch_environment`` injects the layer snapshot (tests, hosts that
        # register .env layers); None falls back to launch_environment_of(ctx),
        # which is the live process environment when the host registered none.
        self._launch_environment = launch_environment
        self.text: Optional[str] = None  # last read/persisted text; None while absent
        self.values: Dict[str, str] = {}
        self.records: Dict[str, Dict[str, Any]] = {}
        self._operations = threading.RLock()  # the exclusive operation chain
        self.closed = False
        self._observer: Any = None
        self._debounce: Optional[threading.Timer] = None
        self._debounce_lock = threading.Lock()

    # ------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Service.init adaptation: boot read, then the optional watcher."""
        self.load_initial()
        if not self.spec["watch"]:
            return
        self._start_watcher()

    def close(self) -> None:
        """Dispose: refuse new writes, stop the watcher, let in-flight work finish."""
        self.closed = True
        with self._debounce_lock:
            if self._debounce is not None:
                self._debounce.cancel()
                self._debounce = None
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
        with self._operations:
            pass  # in-flight operations finish before close returns

    def is_closed(self) -> bool:
        """Opaque read of ``closed`` (control flow cannot narrow it across calls)."""
        return self.closed

    def _start_watcher(self) -> None:
        from watchdog.events import FileSystemEventHandler  # type: ignore[import-untyped]
        from watchdog.observers import Observer  # type: ignore[import-untyped]

        provider = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):  # type: ignore[no-untyped-def]
                if provider.closed:
                    return
                src = getattr(event, "src_path", None)
                dest = getattr(event, "dest_path", None)
                target = provider.spec["filename"]
                if src != target and dest != target:
                    return
                provider._schedule_refresh()

        canonicalize_watch_path(self.spec["filename"])  # proves the hierarchy is valid
        watched = os.path.dirname(self.spec["filename"])
        while watched and not os.path.isdir(watched):
            parent = os.path.dirname(watched)
            if parent == watched:
                return  # no existing ancestor to watch; the writer will create it
            watched = parent
        observer = Observer()
        observer.daemon = True
        # Recursive like chokidar's default over the canonicalized ancestor:
        # the document directory may not exist yet at start, and the handler
        # below filters every event down to this provider's own filename.
        observer.schedule(_Handler(), watched, recursive=True)
        observer.start()
        self._observer = observer
        # ready-equivalent: one reconciling pass once the watcher is live.
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        """Coalescing debounce (chokidar awaitWriteFinish adaptation)."""
        delay = max(self.spec["debounceMs"], 0) / 1000.0
        with self._debounce_lock:
            if self.closed:
                return
            if self._debounce is not None:
                self._debounce.cancel()
            timer = threading.Timer(delay, self._refresh_from_timer)
            timer.daemon = True
            self._debounce = timer
            timer.start()

    def _refresh_from_timer(self) -> None:
        with self._debounce_lock:
            self._debounce = None
        try:
            with self._operations:
                self.refresh()
        except Exception as error:  # the queue's error surface: log, never crash the timer
            self.logger.error("credentials-local: reload commit failed at %s", self.spec["filename"])
            self.logger.error("credentials-local: %r", error)

    def queue_refresh(self) -> None:
        """Queue a reload; only an invariant violation escapes (logged, upstream-equal)."""
        self._schedule_refresh()

    # ---------------------------------------------------------- environment

    def _environment(self) -> Any:
        if self._launch_environment is not None:
            return self._launch_environment
        return launch_environment_of(self.ctx)

    def inherited(self, ref: str) -> Optional[str]:
        """The inherited-environment value, or None when empty or unset."""
        entry = self._environment().get_from(ref, ["process"])
        return entry.value if entry is not None and len(entry.value) > 0 else None

    def dotenv_fallback(self, ref: str) -> Optional[Any]:
        """The ``.env`` fallback — below the managed store, never above it."""
        entry = self._environment().get_from(ref, ["project-env", "user-env"])
        return entry if entry is not None and len(entry.value) > 0 else None

    # ------------------------------------------------------------ read side

    def resolve(self, ref: str) -> Optional[Dict[str, str]]:
        inherited = self.inherited(ref)
        if inherited is not None:
            return {"value": inherited, "source": "env"}
        stored = self.values.get(ref)
        if stored is not None:
            return {"value": stored, "source": "file"}
        fallback = self.dotenv_fallback(ref)
        if fallback is not None:
            return {"value": fallback.value, "source": fallback.source}
        return None

    def describe(self, ref: str) -> Dict[str, Any]:
        if self.inherited(ref) is not None:
            return {"configured": True, "source": "env", "writable": False}
        if self.values.get(ref) is not None:
            return {"configured": True, "source": "file", "writable": True}
        fallback = self.dotenv_fallback(ref)
        if fallback is not None:
            return {"configured": True, "source": fallback.source, "writable": True}
        return {"configured": False, "writable": True}

    def list_refs(self) -> List[str]:
        """Stored reference names (never values) — native metadata addition for
        host projections; the environment layers are not enumerable."""
        return list(self.values.keys())

    def read_record(self, key: str) -> Optional[Dict[str, Any]]:
        stored = self.records.get(key)
        # Deep copy: a caller mutating the result must never reach the store
        # (native hardening; upstream hands out the parsed object itself).
        return None if stored is None else copy.deepcopy(stored)

    def describe_record(self, key: str) -> Dict[str, Any]:
        stored = self.records.get(key)
        if stored is None:
            return {"configured": False, "writable": True}
        return {"configured": True, "kind": stored["kind"], "writable": True}

    def list_records(self) -> List[Dict[str, Any]]:
        return [{"key": parse_credential_key(key), "kind": record["kind"]}
                for key, record in self.records.items()]

    # ----------------------------------------------------------- write side

    def set(self, ref: str, value: str) -> None:
        if len(value) == 0:
            raise CredentialsDocumentError(
                f'credentials-local: an empty value cannot be stored for "{ref}"; use unset')
        self._write(ref, value)

    def unset(self, ref: str) -> None:
        self._write(ref, None)

    def _write(self, ref: str, value: Optional[str]) -> None:
        """Queue one line edit; entry checks reject early, the queue re-judges at run time."""
        credential_ref(ref)
        verb = "unset" if value is None else "set"
        self._assert_open(verb, ref)
        self._assert_unshadowed(ref, verb)
        with self._operations:
            self._assert_open(verb, ref, queued=True)
            self._assert_unshadowed(ref, verb)
            self._locked_document_edit(
                lambda: self._write_locked(ref, value, verb))

    def _write_locked(self, ref: str, value: Optional[str], verb: str) -> None:
        self.reconcile_from_disk()
        existing = self.values.get(ref)
        if value is None and existing is None:
            return  # unset of an absent ref is a no-op
        next_text = render_ref(self.text, ref, value)
        self._persist(next_text)
        if value is None:
            self.values.pop(ref, None)
        else:
            self.values[ref] = value
        self.notify_updated(ref)

    def modify_record(self, key: str, mutate: Callable[[Optional[Dict[str, Any]]],
                                                       Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
        parse_credential_key(key)
        self._assert_open("modify", key)
        with self._operations:
            self._assert_open("modify", key, queued=True)
            return self._locked_document_edit(lambda: self._modify_record_locked(key, mutate))

    def _modify_record_locked(self, key: str, mutate: Callable[[Optional[Dict[str, Any]]],
                                                               Optional[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
        self.reconcile_from_disk()
        current = self.records.get(key)
        current_copy = None if current is None else copy.deepcopy(current)
        outcome = mutate(current_copy)
        if outcome is None:
            return current_copy  # the caller's decision was a no-op
        next_record = copy.deepcopy(dict(outcome))  # isolate the store from the caller's objects
        if next_record.get("kind") == "grant":
            assert_json_value(f'record "{key}" payload', next_record.get("payload"), set())
        else:
            assert_storable_api_key(key, next_record)
        # Boot-equivalent admission at the write boundary: a mutation can never
        # persist a document the next boot would reject (upstream states this
        # principle for api-key records; native applies it to every tag).
        _parse_record(key, next_record, f"<write:{key}>")
        next_text = render_record(self.text, key, next_record)
        self._persist(next_text)
        self.records[key] = next_record
        self.notify_record_updated(key)
        return copy.deepcopy(next_record)

    def delete_record(self, key: str) -> None:
        parse_credential_key(key)
        self._assert_open("delete", key)
        with self._operations:
            self._assert_open("delete", key, queued=True)
            self._locked_document_edit(lambda: self._delete_record_locked(key))

    def _delete_record_locked(self, key: str) -> None:
        self.reconcile_from_disk()
        if key not in self.records:
            return
        next_text = render_record(self.text, key, None)
        self._persist(next_text)
        self.records.pop(key, None)
        self.notify_record_updated(key)

    def _assert_open(self, verb: str, subject: str, queued: bool = False) -> None:
        if not self.closed:
            return
        if queued:
            raise CredentialsDocumentError(
                f'credentials-local was disposed before the queued "{subject}" {verb} ran')
        raise CredentialsDocumentError(f'credentials-local is disposed: cannot {verb} "{subject}"')

    def _assert_unshadowed(self, ref: str, verb: str) -> None:
        """Only the inherited environment can shadow a write into apparent no-effect."""
        if self.inherited(ref) is not None:
            raise CredentialsDocumentError(
                f'credentials-local: "{ref}" is supplied read-only by the launching environment, '
                f"so {verb} would be shadowed; unset it in the shell you start dsh from instead")

    def _locked_document_edit(self, operation: Callable[[], Any]) -> Any:
        """mkdir 0700 → cross-process writer lock → operation (re-read first)."""
        directory = os.path.dirname(self.spec["filename"])
        os.makedirs(directory, mode=0o700, exist_ok=True)
        return with_file_lock(self.spec["filename"], operation, wait_ms=DOCUMENT_LOCK_WAIT_MS)

    def _persist(self, next_text: str) -> None:
        write_file_atomic(self.spec["filename"], next_text, mode=0o600, dir_mode=0o700)
        self.text = next_text

    # ----------------------------------------------------------- load side

    def load_initial(self) -> None:
        """Boot read: absent file = empty store; an invalid one fails activation.

        A credentials document that exists but cannot be trusted must never be
        treated as "no credentials stored". The recognized pre-release flat
        layout is upgraded in place first.
        """
        assert_owner_only(self.spec["filename"])
        try:
            with open(self.spec["filename"], "r", encoding="utf-8") as handle:
                text = handle.read()
        except OSError as error:
            if not _is_absent(error):
                raise
            return
        if render_flat_layout_migration(text) is not None:
            text = self._migrate_flat_document()
        document = parse_credentials_document(text, self.spec["filename"])
        self.values = document["refs"]
        self.records = document["records"]
        self.text = text

    def _migrate_flat_document(self) -> str:
        """One-shot upgrade under the writer lock; re-reads first (a concurrent
        boot may have migrated already); values are carried verbatim."""
        def migrate() -> str:
            with open(self.spec["filename"], "r", encoding="utf-8") as handle:
                current = handle.read()
            migrated = render_flat_layout_migration(current)
            if migrated is None:
                return current
            write_file_atomic(self.spec["filename"], migrated, mode=0o600, dir_mode=0o700)
            self.logger.info("credentials-local: migrated %s to the version %d layout; values are unchanged",
                             self.spec["filename"], DOCUMENT_VERSION)
            return migrated

        directory = os.path.dirname(self.spec["filename"])
        os.makedirs(directory, mode=0o700, exist_ok=True)
        return with_file_lock(self.spec["filename"], migrate, wait_ms=DOCUMENT_LOCK_WAIT_MS)

    def refresh(self) -> None:
        """Watcher reload: unchanged content no-ops; unreadable keeps last good."""
        if self.closed:
            return
        try:
            self.reconcile_from_disk()
        except Exception as error:
            if getattr(error, "code", None) == "INVARIANT":
                raise
            self.logger.warning("credentials-local: reload failed at %s; keeping the last good document",
                                self.spec["filename"])
            self.logger.warning("credentials-local: %r", error)

    def reconcile_from_disk(self) -> None:
        """Compare on-disk text against the cache and publish any difference.

        Absence publishes the empty store; an unreadable or invalid document
        throws, so each caller picks its policy — a reload warns and keeps the
        last good snapshot, a write fails loud rather than overwriting a
        document it could not understand.
        """
        assert_owner_only(self.spec["filename"])
        try:
            with open(self.spec["filename"], "r", encoding="utf-8") as handle:
                text: Optional[str] = handle.read()
        except OSError as error:
            if not _is_absent(error):
                raise
            text = None
        if text == self.text or self.is_closed():
            return
        next_document = ({"refs": {}, "records": {}} if text is None
                         else parse_credentials_document(text, self.spec["filename"]))
        changed_refs = _changed_refs(self.values, next_document["refs"])
        changed_records = _changed_records(self.records, next_document["records"])
        self.text = text
        self.values = next_document["refs"]
        self.records = next_document["records"]
        for ref in changed_refs:
            self.notify_updated(ref)
        for key in changed_records:
            self.notify_record_updated(key)

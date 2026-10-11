"""Per-harness-home anonymous user id shared by telemetry and feedback.

Port of ``@deepseek-ai/dsh-anonymous-user-id`` (commit 5badb15). Contract kept:
- The id is a random UUID persisted as a bare line in ``.anonymous-user-id``
  inside the harness home (``$DSH_HOME`` > ``~/.dsh``), and NEVER derived from
  the hostname, network address, git remote, or any other identifying source.
- Scoped to the harness home, not the machine: every process sharing one
  ``$DSH_HOME`` reports the same id; deleting the file mints a fresh identity
  on the next launch.
- Memoized per resolved file path: one process touches the disk once, and a
  file deleted mid-run keeps the process's id until the next launch. Distinct
  test homes never share an id.
- Concurrent first launch uses the existing cross-process writer lock and
  atomic replacement. Contenders recheck the file under that lock; none can
  rewrite a winner's half-written identity. The process memo is synchronized.
- Persistence is best-effort: a write failure (read-only home) still returns
  a usable id for the current run, so feedback and telemetry are never blocked.

Python adaptation: ``writeFileSync(flag: "wx")`` becomes ``os.open`` with
``O_CREAT | O_EXCL``; the memo is a module-level dict (process lifetime),
same as the upstream Map.
"""
from __future__ import annotations

import os
import re
import uuid as uuid_module
import threading
from ..dshutil.atomic_write import with_file_lock, write_file_atomic
from typing import Any, Callable, Dict, Optional

from ..dshutil.home_paths import resolve_dsh_home

ANONYMOUS_USER_ID_FILE_NAME = ".anonymous-user-id"
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
                          re.IGNORECASE)

# Process-lifetime memo keyed by resolved file path, so distinct test homes
# never share an id.
_memo: Dict[str, str] = {}
_memo_lock = threading.RLock()


def read_persisted_id(file: str) -> Optional[str]:
    """A valid persisted id from the file, or None when absent/corrupt."""
    try:
        with open(file, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return None
    value = text.strip()
    return value if UUID_PATTERN.match(value) else None


def get_or_create_anonymous_user_id(options: Optional[Dict[str, Any]] = None) -> str:
    """The harness home's anonymous id, created and persisted on first use."""
    options = options or {}
    env = options.get("env")
    file = os.path.join(resolve_dsh_home(None, env), ANONYMOUS_USER_ID_FILE_NAME)
    with _memo_lock:
        cached = _memo.get(file)
        if cached is not None:
            return cached
        generator: Callable[[], Any] = options.get("random_uuid") or (lambda: str(uuid_module.uuid4()))
        identified = read_persisted_id(file)
        if identified is None:
            created = str(generator())
            try:
                os.makedirs(os.path.dirname(file), exist_ok=True)
                def settle() -> str:
                    # Recheck under the cross-process lock, then publish only
                    # complete content. A losing reader never rewrites a file
                    # in the winner's create-to-write window.
                    existing = read_persisted_id(file)
                    if existing is not None: return existing
                    write_file_atomic(file, created + "\n")
                    return created
                identified = with_file_lock(file, settle)
            except OSError:
                identified = read_persisted_id(file) or created
        _memo[file] = identified
        return identified

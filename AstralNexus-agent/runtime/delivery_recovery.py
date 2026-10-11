"""Resume publishes a crashed host left half-done, and verify what landed.

``runtime/workspace_publish.py`` has carried ``recover_pending`` and
``read_binding`` since the delivery contract landed, and nothing in production
called either of them.  Two consequences, both observed:

- An interrupted publish left a pending journal and a half-published tree that
  no one resumed.  Live fix4: 43 correct deliverables were published into the
  *run dir* because the publish root was wrong; the only way to find that out
  was for a human to read the journal by hand.
- A publication the worker *reported* was never checked against what actually
  landed on disk -- so "published" was the worker's word alone.

DeerFlow treats the first as orphan recovery and is explicit that only the
recovery signal may end a run whose producer is gone: ``runtime/runs/manager.py``
keeps ``ORPHAN_RECOVERY_STOP_REASON = "orphan_recovered"`` and writes
``stop_reason`` atomically with the terminal status.  This module is the same
idea for our delivery journals, and the reason codes below are the single source
for it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import workspace_publish

#: Outcome codes, one source.  Rendered into events and asserted by tests.
RESUMED: str = "delivery_orphan_resumed"
ROLLED_BACK: str = "delivery_orphan_rolled_back"
UNREADABLE: str = "delivery_orphan_unreadable"
BINDING_VERIFIED: str = "delivery_binding_verified"
BINDING_UNVERIFIED: str = "delivery_binding_unverified"

RESULT_DESCRIPTIONS: dict[str, str] = {
    RESUMED: "上次发布被中断，已按 journal 续完。",
    ROLLED_BACK: "上次发布被中断且存活字节不自洽，已按 journal 回滚。",
    UNREADABLE: "journal 存在但无法据此恢复；未改动任何文件。",
    BINDING_VERIFIED: "发布结果已按工作区里的 binding 独立核验。",
    BINDING_UNVERIFIED: "worker 报告已发布，但工作区里读不到可核验的 binding。",
}


def candidate_roots(
    home: Path,
    sessions: Iterable[Mapping[str, Any]] = (),
) -> list[Path]:
    """Every root a publish could have targeted, deduplicated and existing.

    Three kinds, because the publish root has legitimately been all three:
    the session workspace (where it belongs, after the 2026-10-07 fix), the
    host home, and individual run dirs (where it went when the workspace env
    var was rewritten -- fix4's 43 files live under one of those).
    """
    home = Path(home)
    roots: list[Path] = [home]
    for session in sessions or ():
        for key in ("workspace", "run_dir"):
            value = str(session.get(key) or "").strip()
            if value:
                roots.append(Path(value))
    try:
        roots.extend(sorted(home.glob("chat/*/*/runs/task_task-*")))
    except OSError:
        pass

    seen: list[Path] = []
    for candidate in roots:
        try:
            resolved = candidate.expanduser().resolve()
        except OSError:
            continue
        if resolved.is_dir() and resolved not in seen:
            seen.append(resolved)
    return seen


def journals(root: Path) -> list[Path]:
    """The publish journals directly under ``root`` (the layout is fixed)."""
    delivery_dir = Path(root) / workspace_publish.DELIVERY_DIRNAME
    if not delivery_dir.is_dir():
        return []
    prefix = workspace_publish.JOURNAL_PREFIX
    return sorted(
        path for path in delivery_dir.glob(prefix + "*.json")
        if path.is_file()
    )


def delivery_id_of(journal: Path) -> str:
    """``journal_<delivery_id>.json`` -> ``<delivery_id>``."""
    name = Path(journal).name
    prefix = workspace_publish.JOURNAL_PREFIX
    if name.startswith(prefix):
        name = name[len(prefix):]
    if name.endswith(".json"):
        name = name[:-len(".json")]
    return name


def sweep(
    home: Path,
    sessions: Iterable[Mapping[str, Any]] = (),
    *,
    recover: Callable[..., Any] | None = None,
) -> list[dict[str, Any]]:
    """Resume or roll back every pending publish under the candidate roots.

    ``recover`` is injectable so a test can drive the sweep without a real
    journal; production passes nothing and gets ``recover_pending``.
    """
    recover = recover or workspace_publish.recover_pending
    outcomes: list[dict[str, Any]] = []
    for root in candidate_roots(home, sessions):
        for journal in journals(root):
            delivery_id = delivery_id_of(journal)
            try:
                result = recover(workspace_root=root, delivery_id=delivery_id)
            except Exception as exc:  # noqa: BLE001 - one bad journal must not stop the sweep
                outcomes.append({
                    "outcome": UNREADABLE,
                    "root": str(root),
                    "delivery_id": delivery_id,
                    "journal": str(journal),
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                })
                continue
            rolled_back = bool(getattr(result, "rolled_back", False))
            outcomes.append({
                "outcome": ROLLED_BACK if rolled_back else RESUMED,
                "root": str(root),
                "delivery_id": delivery_id,
                "journal": str(journal),
                "run_dir": str(getattr(result, "run_dir", "") or ""),
                "manifest_sha256": str(getattr(result, "manifest_sha256", "") or ""),
                "published": len(getattr(result, "published", ()) or ()),
            })
    return outcomes


def verify_published(
    *,
    workspace_root: Path,
    delivery_id: str,
    expect_manifest_sha256: str = "",
) -> dict[str, Any]:
    """Check a reported publication against the binding actually on disk.

    Reads the *workspace*, never the worker's claim -- the same separation the
    project applies to reports: a claim is not evidence, the artifact is.
    Returns a verdict dict; never raises, so a caller can record it verbatim.
    """
    if not delivery_id:
        return {"outcome": BINDING_UNVERIFIED, "reason": "no delivery_id reported"}
    try:
        binding = workspace_publish.read_binding(Path(workspace_root), delivery_id)
    except Exception as exc:  # noqa: BLE001 - an unreadable binding is a verdict, not a crash
        return {
            "outcome": BINDING_UNVERIFIED,
            "reason": f"{type(exc).__name__}: {exc}"[:500],
            "delivery_id": delivery_id,
        }
    if not isinstance(binding, dict) or not binding:
        return {"outcome": BINDING_UNVERIFIED, "reason": "binding empty or not a mapping",
                "delivery_id": delivery_id}
    recorded = str(binding.get(workspace_publish.MANIFEST_SHA256_FIELD) or "")
    verdict: dict[str, Any] = {
        "outcome": BINDING_VERIFIED,
        "delivery_id": delivery_id,
        "manifest_sha256": recorded,
        "files": len(binding.get("files") or {}),
    }
    if expect_manifest_sha256 and recorded and recorded != expect_manifest_sha256:
        verdict["outcome"] = BINDING_UNVERIFIED
        verdict["reason"] = (
            f"binding manifest {recorded!r} != reported {expect_manifest_sha256!r}"
        )
    return verdict
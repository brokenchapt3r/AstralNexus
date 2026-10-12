"""Detect repeated action/evidence cycles, independently of tool success."""
from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Optional


class ProgressGuard:
    def __init__(self) -> None:
        self.history: list[str] = []
        self.warned_cycles: set[str] = set()
        self.file_version: str | None = None
        self.seen: set[str] = set()

    @staticmethod
    def digest(value: Any) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()

    def observe(self, call: dict[str, Any], result: dict[str, Any], files: dict[str, Any]) -> dict[str, Any] | None:
        version = self.digest(files)
        if version != self.file_version:
            self.file_version = version
            self.history.clear()
            self.warned_cycles.clear()
            self.seen.clear()
        function = call.get("function") or {}
        arguments = function.get("arguments") or {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                pass
        # Do not treat provider IDs, clock ticks or elapsed time as new evidence.
        evidence = {key: result[key] for key in (
            "status", "returncode", "content", "output", "stdout", "stderr", "error",
            "path", "truncated", "status_code", "body",
        ) if key in result}
        fingerprint = self.digest([function.get("name"), arguments, evidence])
        if fingerprint not in self.seen:
            self.seen.add(fingerprint)
            self.warned_cycles.clear()
            self.history.clear()
        self.history.append(fingerprint)
        self.history = self.history[-64:]
        for width in range(1, len(self.history) // 2 + 1):
            cycle = self.history[-width:]
            if cycle != self.history[-2 * width:-width]:
                continue
            # Rotation-invariant identity: starting at another file is the same loop.
            signature = self.digest(min(tuple(cycle[i:] + cycle[:i]) for i in range(width)))
            repeated = signature in self.warned_cycles
            self.warned_cycles.add(signature)
            self.history.clear()
            return {"action": "handoff" if repeated else "replan", "cycle_id": signature,
                    "cycle_length": width, "reason": "repeated_actions_with_unchanged_evidence_and_owned_files"}
        return None


def _env_fraction(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.01, value))


class WriteProgressGuard:
    """Zero-write budget burn detector for mutation contracts (P2).

    ``ProgressGuard`` above catches REPEATED identical action/evidence
    cycles.  Live r13c proved the complementary failure mode it cannot see:
    node N-125 spent its whole execution on ~70 *diverse* read-only calls —
    no cycle ever repeated, so no fingerprint matched — and submitted a
    zero-write confession at the end; N-123/N-126 burned their wall-clock
    caps building drafts entirely in context and lost everything at the wall.

    DeerFlow answers this class with ``loop_capped`` stop reasons and capped
    semantics that distinguish productive from unproductive runs
    (status_contract: capped WITH usable output stays usable; capped without
    output fails with a structured stop_reason).  This guard is the
    AstralNexus port for the mutation side: it watches the consumed budget
    fraction against owned-file version changes (the execution ledger's
    filesystem digest — real bytes, not model claims) and responds in three
    bounded steps:

    * ``nudge`` at 50% — start landing incremental writes now;
    * ``final_warning`` at 70% — last chance before tool withdrawal;
    * ``cap_text_only`` at 85% — tools withdrawn, honest summary forced.

    Capping at 85% instead of dying at 100% converts a wall-clock death
    (in-context work lost, receipts stranded) into an early capped
    submission with every receipt preserved — strictly more recoverable.
    A worker that HAS written owned bytes never hears from this guard.
    Read-only contracts (empty owned_paths) never instantiate it.
    """

    def __init__(
        self,
        budget: Any,
        *,
        nudge_fraction: float = 0.5,
        warn_fraction: float = 0.7,
        cap_fraction: float = 0.85,
        rolling: bool = False,
    ) -> None:
        self.budget = budget
        self.rolling = rolling
        self._progress_fraction = 0.0
        self.nudge_fraction = min(1.0, max(0.01, float(nudge_fraction)))
        self.warn_fraction = min(1.0, max(self.nudge_fraction, float(warn_fraction)))
        self.cap_fraction = min(1.0, max(self.warn_fraction, float(cap_fraction)))
        self._baseline_version: Optional[str] = None
        self._writes_observed = False
        self._nudged = False
        self._warned = False
        self._capped = False
        self.actions_taken: list[str] = []

    @classmethod
    def from_env(cls, budget: Any, *, rolling: bool = False) -> "WriteProgressGuard":
        return cls(
            budget,
            nudge_fraction=_env_fraction("ASTRALNEXUS_WRITE_PROGRESS_NUDGE", 0.5),
            warn_fraction=_env_fraction("ASTRALNEXUS_WRITE_PROGRESS_WARN", 0.7),
            cap_fraction=_env_fraction("ASTRALNEXUS_WRITE_PROGRESS_CAP", 0.85),
            rolling=rolling,
        )

    def observe(self, files_version: Optional[str]) -> None:
        """Feed the owned-files digest (``ProgressGuard.file_version``).

        The first observation pins the baseline; any later change means real
        bytes landed inside owned paths and disarms the guard permanently —
        measured filesystem state, never a model claim.
        """
        if not files_version:
            return
        if self._baseline_version is None:
            self._baseline_version = files_version
            return
        if files_version != self._baseline_version:
            self._writes_observed = True
            if self.rolling:
                self._baseline_version = files_version
                self._progress_fraction = self.budget_fraction()
                self._nudged = self._warned = self._capped = False

    @property
    def writes_observed(self) -> bool:
        return self._writes_observed

    def budget_fraction(self) -> float:
        """Consumed fraction of the binding budget dimension (0..1)."""
        if self.budget is None:
            return 0.0
        try:
            snapshot = self.budget.snapshot()
        except Exception:
            return 0.0
        fractions = []
        max_seconds = float(snapshot.get("max_seconds") or 0)
        if max_seconds > 0:
            fractions.append(float(snapshot.get("elapsed_seconds") or 0) / max_seconds)
        max_calls = int(snapshot.get("max_tool_calls") or 0)
        if max_calls > 0:
            fractions.append(int(snapshot.get("tool_calls_used") or 0) / max_calls)
        max_rounds = int(snapshot.get("max_tool_rounds") or 0)
        if max_rounds > 0:
            fractions.append(int(snapshot.get("tool_rounds_used") or 0) / max_rounds)
        return min(1.0, max(fractions)) if fractions else 0.0

    def check(self) -> Optional[dict[str, Any]]:
        """One bounded escalation per threshold; None when nothing is due."""
        if (self._writes_observed and not self.rolling) or self.budget is None:
            return None
        fraction = self.budget_fraction()
        if self.rolling:
            fraction = max(0.0, fraction - self._progress_fraction) / max(0.001, 1.0 - self._progress_fraction)
        if fraction >= self.cap_fraction and not self._capped:
            self._capped = True
            self.actions_taken.append("cap_text_only")
            return {
                "action": "cap_text_only",
                "stop_reason": "no_recent_write_capped" if self._writes_observed else "zero_write_capped",
                "budget_fraction": round(fraction, 3),
                "writes_observed": self._writes_observed,
                "reason": "budget_burned_without_recent_owned_file_writes" if self._writes_observed else "budget_burned_without_owned_file_writes",
            }
        if fraction >= self.warn_fraction and not self._warned:
            self._warned = True
            self.actions_taken.append("final_warning")
            return {
                "action": "final_warning",
                "budget_fraction": round(fraction, 3),
                "writes_observed": self._writes_observed,
                "reason": "budget_burned_without_owned_file_writes",
            }
        if fraction >= self.nudge_fraction and not self._nudged:
            self._nudged = True
            self.actions_taken.append("nudge")
            return {
                "action": "nudge",
                "budget_fraction": round(fraction, 3),
                "writes_observed": self._writes_observed,
                "reason": "budget_burned_without_owned_file_writes",
            }
        return None


def write_progress_feedback(signal: dict[str, Any]) -> str:
    """Model-visible feedback text for a WriteProgressGuard signal."""
    percent = int(round(float(signal.get("budget_fraction") or 0.0) * 100))
    action = str(signal.get("action") or "")
    if signal.get("writes_observed") and action != "cap_text_only":
        return (f"自上次落盘改动后，剩余执行预算已消耗 {percent}%，没有新的文件修改。"
                "请依据已有执行失败证据直接进行针对性修改并复验，避免重复调查；若没有可行修复，明确报告阻塞。")
    if action == "cap_text_only":
        if signal.get("writes_observed"):
            return (
                f"自上次落盘改动后，剩余执行预算已消耗 {percent}%，没有新的文件修改。"
                "工具已被收回（stop_reason=no_recent_write_capped）。如实总结已有修改、失败执行证据和未完成项；不得声称验收通过。"
            )
        return (
            f"执行预算已消耗 {percent}%，但 owned 路径下没有任何经实测的落盘改动。"
            "工具已被收回（stop_reason=zero_write_capped）。请立即用纯文本如实总结："
            "已完成的分析与诊断（逐条给出文件路径、行号等证据引用）、已构建但未落盘的内容要点、"
            "未完成项及确切原因。不得声称验收通过；这段总结将作为本次执行的最终提交，"
            "已持久化的工具收据全部保留并会被独立复验。"
        )
    if action == "final_warning":
        return (
            f"警告：执行预算已消耗 {percent}%，owned 路径下仍没有任何落盘改动。"
            "这是最后一次提醒：立即停止扩张分析范围，把已完成的部分用 file_write 增量写入 "
            "owned 文件（小步多次，每次一个文件或一段），分析结论也应写入工作笔记。"
            f"预算达到 {int(round(0.85 * 100))}% 仍无落盘时，工具将被收回并强制进入如实总结。"
        )
    return (
        f"提示：执行预算已消耗 {percent}%，owned 路径下尚无落盘改动。"
        "不要只在上下文中构建完整草稿——草稿会随预算耗尽全部丢失。"
        "请从现在开始增量落盘：把已完成的部分写入 owned 文件（小步多次），"
        "后续修改用追加或就地编辑继续；若存在阻塞，如实写明阻塞原因。"
    )

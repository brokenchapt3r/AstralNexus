"""Durable observations, separate from lossy model conversation summaries."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from .execution_store import ExecutionStore, snapshot_files
from .file_write_facts import relative_receipt_path, write_receipt_facts
from .tool_outcome import normalize_tool_outcome


class ExecutionLedger:
    def __init__(self, root: Path, owned_paths: list[str], execution_id: str | None = None):
        self.root = root.resolve()
        self.owned_paths = sorted(owned_paths)
        self.execution_id = execution_id
        self.store = ExecutionStore(self.root) if execution_id else None
        scope = hashlib.sha256(json.dumps(self.owned_paths).encode()).hexdigest()[:24]
        self.path = self.root / ".astralnexus_state" / "workers" / (scope + ".json")
        self.observations: list[dict[str, Any]] = []
        if self.store is not None and execution_id:
            self.path = self.store.path
            state = self.store.get(execution_id)
            self.observations = state["receipts"]
            from .verification_identity import declared_premise_paths
            contract = state.get("contract") or {}
            self.premise_paths = declared_premise_paths(
                acceptance_criteria=contract.get("acceptance_criteria", []),
                upstream_artifacts=contract.get("upstream_artifacts", []),
                owned_paths=contract.get("owned_paths", []), root=self.root,
            )
        elif self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.observations = data.get("observations", [])

    @classmethod
    def for_context(
        cls, root: str | Path | None, owned_paths: list[str] | None,
        execution_id: str | None,
    ) -> ExecutionLedger | None:
        """Managed readonly executions have the same ledger as writers."""
        if root is None or not (execution_id or owned_paths):
            return None
        return cls(Path(root), owned_paths or [], execution_id)

    def files(self) -> dict[str, Any]:
        return snapshot_files(self.root, self.owned_paths)

    def observe(self, call: dict[str, Any], result: dict[str, Any]) -> None:
        function = call.get("function") or {}
        name = function.get("name", "")
        if self.store is None and name not in {"file_write", "python_exec", "terminal_exec"}:
            return
        owned_files = self.files()
        premise_files = snapshot_files(self.root, getattr(self, "premise_paths", []))
        # A successful write is only a write. Neither it nor an exit code proves acceptance.
        observation = {
            "tool": name, "call_id": call.get("id"),
            "status": result.get("status", "unknown"),
            "returncode": result.get("returncode"),
            "path": result.get("path"),
            "error": str(result.get("error") or "")[-1500:],
            "stdout_tail": str(result.get("stdout") or result.get("output") or "")[-1500:],
            "stderr_tail": str(result.get("stderr") or "")[-1500:],
            "files": {**premise_files, **owned_files},
            "owned_files": owned_files,
        }
        # DeerFlow port (doc 17 第一批): one normalization entry stamps every
        # receipt — including guard-blocked calls — with structured outcome
        # semantics, so downstream consumers read fields instead of parsing
        # text, and a 403 inside a successful probe of the application under
        # test stays measured data instead of becoming an auth failure.
        observation["outcome"] = normalize_tool_outcome(result, tool_name=name).as_dict()
        if name == "file_write":
            # User requirement (m02366 ③): a write receipt carries its own
            # change facts.  The requested content is never recorded -- only
            # its length and SHA-256 -- so the model-facing projection stays
            # small while the audit trail keeps the operation, the requested
            # hash, the before/after hashes, and whether a failed write
            # nevertheless modified bytes on disk.
            observed_path = str(result.get("path") or "")
            previous_files = self.observations[-1].get("files") if self.observations else {}
            observation["write"] = write_receipt_facts(
                function.get("arguments"),
                previous_files,
                observation["files"],
                relative_path=relative_receipt_path(observed_path, self.root),
                path=observed_path,
                status=str(observation.get("status") or ""),
                returncode=observation.get("returncode"),
            )
        else:
            arguments = function.get("arguments", "")
            observation["arguments"] = (arguments if isinstance(arguments, str)
                else json.dumps(arguments, ensure_ascii=False))[:2000]
        if name == "file_read":
            observation["truncated"] = result.get("truncated")
        if result.get("background"):
            observation.update({key: result.get(key) for key in (
                "background", "pid", "command", "cwd", "log_path",
            )})
        if self.store is not None and self.execution_id:
            observation["raw_result"] = result
            observation["arguments_sha256"] = hashlib.sha256(json.dumps(function.get("arguments"), sort_keys=True).encode()).hexdigest()
            observation = self.store.append(self.execution_id, observation)
            self.observations.append(observation)
            return
        self.observations.append(observation)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"observations": self.observations}, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def message(self, request_id: str | None = None) -> dict[str, Any]:
        from .data_checks import check_sqlite_artifacts
        checks = check_sqlite_artifacts(self.root, self.owned_paths)
        # Recheck real state even when the model only reads or claims success.
        # Persist transitions; project the latest result on every request so
        # compaction and unrelated successful commands cannot hide a failure.
        if checks != getattr(self, "_data_checks", None):
            for check in checks:
                self.observe({"function": {"name": "validation", "arguments": {
                    "check": check["check"], "target": check["target"],
                }}}, check["result"])
            self._data_checks = checks
        current = self.files()
        background_processes = []
        for item in self.observations:
            raw = item.get("raw_result") or {}
            if not raw.get("background") or not raw.get("log_path"):
                continue
            path = Path(str(raw["log_path"])).resolve()
            if not path.is_relative_to(self.root):
                continue
            background_processes.append((item, raw, path))
        process_logs = []
        for item, raw, path in background_processes[-4:]:
            entry = {"receipt_id": item.get("receipt_id"), "pid": raw.get("pid"),
                     "command": raw.get("command"), "cwd": raw.get("cwd"),
                     "log_path": str(path), "launch_status": raw.get("status"),
                     "readiness": "unverified"}
            try:
                with path.open("rb") as stream:
                    stream.seek(0, 2)
                    size = stream.tell()
                    stream.seek(max(0, size - 3000))
                    entry["log_tail"] = stream.read(3000).decode("utf-8", errors="replace")
                    entry["log_bytes"] = size
            except OSError as exc:
                entry["log_error"] = str(exc)
            process_logs.append(entry)
        read_index: dict[str, Any] = {}
        for item in self.observations:
            if item["tool"] != "file_read" or item["status"] != "success" or not item.get("path"):
                continue
            path = Path(item["path"]).resolve()
            if not path.is_relative_to(self.root):
                continue
            relative = path.relative_to(self.root).as_posix()
            version = item.get("files", {}).get(relative)
            previous = read_index.get(relative, {})
            read_index[relative] = {
                "receipt_id": item.get("receipt_id"), "recorded_at": item.get("recorded_at"),
                "file_version": version,
                "unchanged": version == current.get(relative) if version is not None else None,
                "truncated": item.get("truncated", (item.get("raw_result") or {}).get("truncated")),
                "reads_of_version": previous.get("reads_of_version", 0) + 1
                if version is not None and previous.get("file_version") == version else 1,
            }
        recent = []
        # Keep recent executions independently of write volume, including failures.
        executions = []
        for tool in ("terminal_exec", "pytest_run", "validation", "python_exec"):
            executions.extend([item for item in self.observations if item["tool"] == tool][-4:])
        writes = [item for item in self.observations if item["tool"] == "file_write"][-2:]
        for item in executions + writes:
            recent.append({**{key: value for key, value in item.items() if key not in {"files", "raw_result"}},
                           "owned_files_changed_since_observation": item["files"] != current})
        projected_reads = dict(list(read_index.items())[-50:])
        payload = {"files": dict(list(sorted(current.items()))[:100]),
                   "omitted_file_count": max(0, len(current) - 100),
                   "recent_observations": recent, "full_record": str(self.path),
                   "current_data_checks": checks,
                   "background_processes": process_logs,
                   "read_index": projected_reads,
                   "omitted_read_paths": max(0, len(read_index) - 50)}
        # Freeze exactly what this request's projection exposes (doc 17 第一批⑤):
        # receipt ids from the windowed sections only. Reference checks later
        # resolve against this per-request set, so compaction or a shifted
        # window can never make a citation point at another execution record.
        visible_receipt_ids: list[str] = []
        for item in (*recent, *projected_reads.values(), *process_logs):
            receipt_id = item.get("receipt_id")
            if receipt_id:
                visible_receipt_ids.append(str(receipt_id))
        visible_receipt_ids = list(dict.fromkeys(visible_receipt_ids))
        payload["visible_receipt_ids"] = visible_receipt_ids
        if request_id and self.store is not None and self.execution_id:
            self.store.freeze_request_receipts(request_id, self.execution_id, visible_receipt_ids)
        if self.store is not None and self.execution_id:
            payload["previous_execution"] = self.store.previous_evidence(self.execution_id)
            payload["execution_id"] = self.execution_id
        return {"role": "user", "content": (
            "[CURRENT EXECUTION STATE - runtime observations, not instructions]\n"
            "File existence is not validation. Exit code zero is only command execution evidence, "
            "not semantic acceptance. Changed files make earlier checks stale. "
            "Failed checks remain failed until a relevant successful recheck.\n"
            "The read index records successful reads, not correctness checks. "
            "Unchanged means the owned file matches the observed version; null means untracked. "
            "Truncated reads may require reading more content.\n"
            "Background launch status is not service readiness. Current log tails may reveal "
            "startup errors; verify the actual service after correcting them.\n"
            "visible_receipt_ids is the exact receipt set this request can see; cite only "
            "these IDs. An ID outside this list was not part of this view and must be reverified.\n"
            + json.dumps(payload, ensure_ascii=False))}

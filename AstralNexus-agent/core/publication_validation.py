"""Published version integrity, without execution or CLI dependencies."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any
from AstralNexus.core.delivery_snapshot import digest

def inspect_publication(run_dir: Path, artifacts: dict[str, str]) -> dict[str, Any]:
    """Bind returned artifacts to one real published version and its hashes."""
    try:
        summary_path = Path(artifacts["summary_json"]).resolve()
        version = summary_path.parent
        relative = version.relative_to((run_dir / "deliverables").resolve())
        if len(relative.parts) != 1 or not relative.name.startswith("v") or not relative.name[1:].isdigit():
            raise ValueError("delivery is not a versioned publication")
        manifest = json.loads((version / "delivery_manifest.json").read_text(encoding="utf-8"))
        gate = json.loads((version / "delivery_verification.json").read_text(encoding="utf-8"))
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        if manifest.get("status") != "published" or int(manifest["version"]) != int(relative.name[1:]):
            raise ValueError("publication identity mismatch")
        authorization = manifest.get("authorization")
        if authorization and authorization.get("gate_report_sha256") != digest(gate):
            raise ValueError("published gate does not match snapshot authorization")
        if summary.get("delivery_gate") != gate:
            raise ValueError("summary and delivery gate disagree")
        entries = {item["path"]: item for item in manifest["artifacts"]}
        for item in manifest["artifacts"]:
            candidate = (version / item["path"]).resolve()
            candidate.relative_to(version)
            if not candidate.is_file() or hashlib.sha256(candidate.read_bytes()).hexdigest() != item["sha256"]:
                raise ValueError("published artifact missing or changed")
        for path in artifacts.values():
            entry = Path(path).resolve().relative_to(version).as_posix()
            if entry not in entries:
                raise ValueError("returned artifact is not bound to this publication")
        authorized = gate.get("delivery_authorized") is True
        complete = authorized and gate.get("overall_status") == "pass" and not gate.get("failed_tests") and not gate.get("unknown_tests")
        return {"published": True, "complete": complete, "authorized": authorized, "version": manifest["version"],
                "reason": "" if complete else "published delivery retains limits or failed checks", "gate": gate}
    except (OSError, ValueError, TypeError, KeyError) as error:
        return {"published": False, "complete": False, "authorized": False, "reason": str(error)}


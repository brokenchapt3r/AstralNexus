"""Public compatibility entry. Inject runtime dependencies on the instance.

Only _delivery_evidence_visible retains a documented module replacement hook.
Other internal names are legacy named imports, not runtime injection points.
"""
from .orchestration.orchestrator import Orchestrator
from .orchestration.models import ExecutionRunResult
from .orchestration.support import (
    ROOT_CAUSE_TEXT_SUFFIXES,
    _PUBLISH_EXCLUDE_PARTS,
    _PUBLISH_EXCLUDE_SUFFIXES,
    _delivery_evidence_visible,
    _existing_run_id,
    _json_safe,
    declared_output_path_tokens,
    resolve_declared_output,
)

__all__ = ["Orchestrator", "ExecutionRunResult"]

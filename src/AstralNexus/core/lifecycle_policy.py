"""Purpose-specific lifecycle rules; historical evidence is not write authority."""
WRITE_ACTIVE_STATUSES = frozenset({
    "admitted", "waiting_dependencies", "ready", "running", "result_submitted",
    "verifying", "repair_pending", "blocked",
})
RECOVERY_TARGET_STATUSES = frozenset({"failed", "repair_pending", "blocked"})
REPAIR_SUCCESSOR_STATUSES = frozenset({"completed", "superseded", "cancelled", "failed"})
UNAVAILABLE_PRODUCER_STATUSES = frozenset({"superseded", "cancelled", "failed"})


def can_resume_for_repair(status: str) -> bool:
    return status in WRITE_ACTIVE_STATUSES

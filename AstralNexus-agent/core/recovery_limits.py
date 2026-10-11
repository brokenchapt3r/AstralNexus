"""Recovery thresholds have explicit units and independent configuration."""
from dataclasses import dataclass
import os


@dataclass(frozen=True)
class RecoveryLimits:
    no_progress_operations: int
    same_gap_operations: int
    total_operations: int
    tokens: int
    requests: int

    @classmethod
    def from_environment(cls, legacy_operations: int) -> "RecoveryLimits":
        def value(name: str, default: int) -> int:
            return max(0, int(os.environ.get(name, default)))
        fallback = max(1, legacy_operations)
        return cls(
            max(1, value("ASTRALNEXUS_NO_PROGRESS_OPERATIONS", fallback)),
            max(1, value("ASTRALNEXUS_SAME_GAP_OPERATIONS", fallback)),
            max(1, value("ASTRALNEXUS_TOTAL_RECOVERY_OPERATIONS", fallback)),
            value("ASTRALNEXUS_RUN_TOKEN_BUDGET", 0),
            value("ASTRALNEXUS_RUN_REQUEST_BUDGET", 0),
        )

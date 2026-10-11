"""Token usage calibration: bridge estimated and real token counts.

The ContextCompressor uses `_rough_tokens()` (characters/4) to estimate token
counts before sending to the provider. After receiving API responses, the provider
returns exact `prompt_tokens` and `completion_tokens`. This module collects these
measurements and provides a correction factor for future estimates.

Use case: improve `should_compress()` accuracy and budget tracking precision.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, TypedDict


class TokenMeasurementData(TypedDict):
    timestamp: float
    estimated_tokens: int
    real_prompt_tokens: int
    real_completion_tokens: int
    model: str
    correlation: float


class TokenCalibrationData(TypedDict):
    model: str
    sample_count: int
    mean_correlation: float
    min_correlation: float
    max_correlation: float
    std_dev: float
    correction_factor: float
    last_updated: float


@dataclass
class TokenMeasurement:
    """A single measurement comparing estimated vs real token usage."""
    timestamp: float
    estimated_tokens: int
    real_prompt_tokens: int
    real_completion_tokens: int
    model: str
    correlation: float  # estimated / real_prompt_tokens

    def to_dict(self) -> TokenMeasurementData:
        return {
            "timestamp": self.timestamp,
            "estimated_tokens": self.estimated_tokens,
            "real_prompt_tokens": self.real_prompt_tokens,
            "real_completion_tokens": self.real_completion_tokens,
            "model": self.model,
            "correlation": self.correlation,
        }


@dataclass
class TokenCalibrationStats:
    """Aggregated calibration statistics for a model."""
    model: str
    sample_count: int
    mean_correlation: float  # average estimated/real ratio
    min_correlation: float
    max_correlation: float
    std_dev: float
    last_updated: float

    def correction_factor(self) -> float:
        """Return the inverse of mean_correlation for correcting estimates.

        If estimated/real = 0.85 on average, correction_factor = 1/0.85 = 1.176.
        Apply: corrected_estimate = raw_estimate * correction_factor()
        """
        if self.mean_correlation > 0:
            return 1.0 / self.mean_correlation
        return 1.0

    def to_dict(self) -> TokenCalibrationData:
        return {
            "model": self.model,
            "sample_count": self.sample_count,
            "mean_correlation": self.mean_correlation,
            "min_correlation": self.min_correlation,
            "max_correlation": self.max_correlation,
            "std_dev": self.std_dev,
            "correction_factor": self.correction_factor(),
            "last_updated": self.last_updated,
        }


class TokenCalibrator:
    """Collect token usage measurements and provide calibration factors.

    Thread-safe: designed to be shared across ContextCompressor and LLMClient.
    """

    def __init__(self, max_samples_per_model: int = 100):
        self._measurements: Dict[str, List[TokenMeasurement]] = {}
        self._max_samples = max_samples_per_model

    def record_measurement(
        self,
        estimated_tokens: int,
        real_prompt_tokens: int,
        real_completion_tokens: int,
        model: str,
    ) -> Optional[TokenMeasurement]:
        """Record a new measurement comparing estimated vs real token usage.

        Returns the measurement if recorded, None if invalid (real_prompt_tokens <= 0).
        """
        if real_prompt_tokens <= 0 or estimated_tokens <= 0:
            return None

        correlation = estimated_tokens / real_prompt_tokens
        measurement = TokenMeasurement(
            timestamp=time.time(),
            estimated_tokens=estimated_tokens,
            real_prompt_tokens=real_prompt_tokens,
            real_completion_tokens=real_completion_tokens,
            model=model,
            correlation=correlation,
        )

        if model not in self._measurements:
            self._measurements[model] = []

        samples = self._measurements[model]
        samples.append(measurement)

        # Keep only the most recent samples
        if len(samples) > self._max_samples:
            samples.pop(0)

        return measurement

    def get_stats(self, model: str) -> Optional[TokenCalibrationStats]:
        """Get calibration statistics for a model."""
        samples = self._measurements.get(model)
        if not samples:
            return None

        correlations = [m.correlation for m in samples]
        mean_corr = sum(correlations) / len(correlations)

        # Calculate standard deviation
        variance = sum((c - mean_corr) ** 2 for c in correlations) / len(correlations)
        std_dev = variance ** 0.5

        return TokenCalibrationStats(
            model=model,
            sample_count=len(samples),
            mean_correlation=mean_corr,
            min_correlation=min(correlations),
            max_correlation=max(correlations),
            std_dev=std_dev,
            last_updated=samples[-1].timestamp,
        )

    def correct_estimate(self, raw_estimate: int, model: str) -> int:
        """Apply calibration correction to a raw token estimate.

        If calibration data exists for the model, multiply the estimate by
        the correction factor (inverse of mean correlation). Otherwise return
        the raw estimate unchanged.

        Args:
            raw_estimate: Token count from `_rough_tokens()` or similar.
            model: Model name (e.g., "gpt-4", "claude-3-opus").

        Returns:
            Corrected token estimate.
        """
        stats = self.get_stats(model)
        if stats and stats.sample_count >= 5:  # Require at least 5 samples
            factor = stats.correction_factor()
            return int(raw_estimate * factor)
        return raw_estimate

    def get_all_stats(self) -> Dict[str, TokenCalibrationStats]:
        """Get calibration statistics for all models."""
        return {
            model: stats
            for model in self._measurements.keys()
            if (stats := self.get_stats(model)) is not None
        }

    def reset(self, model: Optional[str] = None) -> None:
        """Reset calibration data.

        Args:
            model: If specified, reset only that model. Otherwise reset all.
        """
        if model:
            self._measurements.pop(model, None)
        else:
            self._measurements.clear()

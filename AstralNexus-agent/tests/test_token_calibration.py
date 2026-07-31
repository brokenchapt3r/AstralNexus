"""Tests for token calibration: estimated vs real token correction."""
import time
from AstralNexus.core.token_calibration import TokenCalibrator, TokenMeasurement


class TestTokenMeasurement:
    def test_measurement_creation(self):
        measurement = TokenMeasurement(
            timestamp=time.time(),
            estimated_tokens=100,
            real_prompt_tokens=120,
            real_completion_tokens=50,
            model="gpt-4",
            correlation=100 / 120,
        )
        assert measurement.estimated_tokens == 100
        assert measurement.real_prompt_tokens == 120
        assert measurement.real_completion_tokens == 50
        assert measurement.model == "gpt-4"
        assert abs(measurement.correlation - 0.833) < 0.01

    def test_measurement_to_dict(self):
        measurement = TokenMeasurement(
            timestamp=1000.0,
            estimated_tokens=100,
            real_prompt_tokens=120,
            real_completion_tokens=50,
            model="gpt-4",
            correlation=0.833,
        )
        data = measurement.to_dict()
        assert data["estimated_tokens"] == 100
        assert data["real_prompt_tokens"] == 120
        assert data["model"] == "gpt-4"
        assert abs(data["correlation"] - 0.833) < 0.001


class TestTokenCalibrator:
    def test_record_valid_measurement(self):
        calibrator = TokenCalibrator()
        measurement = calibrator.record_measurement(
            estimated_tokens=100,
            real_prompt_tokens=120,
            real_completion_tokens=50,
            model="gpt-4",
        )
        assert measurement is not None
        assert measurement.estimated_tokens == 100
        assert measurement.real_prompt_tokens == 120

    def test_record_invalid_measurement_zero_tokens(self):
        calibrator = TokenCalibrator()
        measurement = calibrator.record_measurement(
            estimated_tokens=100,
            real_prompt_tokens=0,  # Invalid
            real_completion_tokens=50,
            model="gpt-4",
        )
        assert measurement is None

    def test_record_invalid_measurement_negative_estimate(self):
        calibrator = TokenCalibrator()
        measurement = calibrator.record_measurement(
            estimated_tokens=0,  # Invalid
            real_prompt_tokens=120,
            real_completion_tokens=50,
            model="gpt-4",
        )
        assert measurement is None

    def test_max_samples_limit(self):
        calibrator = TokenCalibrator(max_samples_per_model=5)
        
        # Record 10 measurements
        for i in range(10):
            calibrator.record_measurement(
                estimated_tokens=100 + i,
                real_prompt_tokens=120 + i,
                real_completion_tokens=50,
                model="gpt-4",
            )
        
        stats = calibrator.get_stats("gpt-4")
        assert stats is not None
        assert stats.sample_count == 5  # Should be capped at 5

    def test_get_stats_insufficient_samples(self):
        calibrator = TokenCalibrator()
        stats = calibrator.get_stats("gpt-4")
        assert stats is None

    def test_get_stats_with_samples(self):
        calibrator = TokenCalibrator()
        
        # Record multiple measurements
        calibrator.record_measurement(100, 120, 50, "gpt-4")  # correlation = 0.833
        calibrator.record_measurement(110, 130, 60, "gpt-4")  # correlation = 0.846
        calibrator.record_measurement(90, 110, 40, "gpt-4")   # correlation = 0.818
        
        stats = calibrator.get_stats("gpt-4")
        assert stats is not None
        assert stats.sample_count == 3
        assert stats.model == "gpt-4"
        assert 0.8 < stats.mean_correlation < 0.85
        assert stats.min_correlation < stats.max_correlation
        assert stats.std_dev >= 0

    def test_correction_factor(self):
        calibrator = TokenCalibrator()
        
        # Average correlation = 0.85 means estimates are 85% of real
        calibrator.record_measurement(85, 100, 50, "gpt-4")
        calibrator.record_measurement(85, 100, 50, "gpt-4")
        calibrator.record_measurement(85, 100, 50, "gpt-4")
        calibrator.record_measurement(85, 100, 50, "gpt-4")
        calibrator.record_measurement(85, 100, 50, "gpt-4")
        
        stats = calibrator.get_stats("gpt-4")
        assert stats is not None
        factor = stats.correction_factor()
        # 1 / 0.85 = 1.176
        assert abs(factor - 1.176) < 0.01

    def test_correct_estimate_without_calibration(self):
        calibrator = TokenCalibrator()
        
        # No calibration data, should return raw estimate
        corrected = calibrator.correct_estimate(100, "gpt-4")
        assert corrected == 100

    def test_correct_estimate_with_insufficient_samples(self):
        calibrator = TokenCalibrator()
        
        # Only 3 samples, need at least 5
        calibrator.record_measurement(85, 100, 50, "gpt-4")
        calibrator.record_measurement(85, 100, 50, "gpt-4")
        calibrator.record_measurement(85, 100, 50, "gpt-4")
        
        corrected = calibrator.correct_estimate(100, "gpt-4")
        assert corrected == 100  # Should not apply correction

    def test_correct_estimate_with_calibration(self):
        calibrator = TokenCalibrator()
        
        # 5 samples with average correlation = 0.85
        for _ in range(5):
            calibrator.record_measurement(85, 100, 50, "gpt-4")
        
        corrected = calibrator.correct_estimate(100, "gpt-4")
        # 100 * (1/0.85) = 100 * 1.176 = 117.6
        assert corrected == 117 or corrected == 118

    def test_get_all_stats(self):
        calibrator = TokenCalibrator()
        
        # Add samples for multiple models
        for _ in range(5):
            calibrator.record_measurement(85, 100, 50, "gpt-4")
            calibrator.record_measurement(90, 100, 50, "claude-3")
        
        all_stats = calibrator.get_all_stats()
        assert len(all_stats) == 2
        assert "gpt-4" in all_stats
        assert "claude-3" in all_stats

    def test_reset_specific_model(self):
        calibrator = TokenCalibrator()
        
        for _ in range(5):
            calibrator.record_measurement(85, 100, 50, "gpt-4")
            calibrator.record_measurement(90, 100, 50, "claude-3")
        
        calibrator.reset(model="gpt-4")
        
        assert calibrator.get_stats("gpt-4") is None
        assert calibrator.get_stats("claude-3") is not None

    def test_reset_all(self):
        calibrator = TokenCalibrator()
        
        for _ in range(5):
            calibrator.record_measurement(85, 100, 50, "gpt-4")
            calibrator.record_measurement(90, 100, 50, "claude-3")
        
        calibrator.reset()
        
        assert calibrator.get_stats("gpt-4") is None
        assert calibrator.get_stats("claude-3") is None

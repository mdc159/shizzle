"""Pure-logic tests for the integrity gates (profile v1) on synthetic signals."""

import numpy as np
import pytest

from integrity import (
    GATE_B_LENGTH_TOLERANCE_SAMPLES,
    PROFILE_VERSION,
    dbfs,
    find_offset,
    measure_residual,
    run_gate_a,
    run_gate_b,
)

SR = 44100


def sine(freq: float, seconds: float = 2.0, amp: float = 0.5,
         stereo: bool = True) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    x = amp * np.sin(2 * np.pi * freq * t)
    return np.stack([x, x], axis=1) if stereo else x


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.asarray(x, dtype=np.float64) ** 2)))


class TestMeasureResidual:
    def test_identical_signals_null_is_effectively_infinite(self):
        ref = sine(440.0)
        m = measure_residual(ref, ref.copy())
        # Residual is exactly zero -> dBFS floor (-300); null depth is
        # ref_rms_dbfs - (-300), i.e. > 250 dB for any practical signal.
        assert m["rms_residual_dbfs"] == -300.0
        assert m["peak_residual_dbfs"] == -300.0
        assert m["null_depth_db"] > 250.0
        assert m["offset"] == 0
        assert m["sample_count"] == len(ref)

    def test_known_minus_20db_residual_computed_correctly(self):
        ref = sine(440.0)
        # Residual: uncorrelated tone scaled to exactly -20 dB below ref RMS
        noise = sine(3001.0, amp=1.0)
        noise *= (rms(ref) * 10 ** (-20 / 20)) / rms(noise)
        m = measure_residual(ref, ref + noise)
        assert m["null_depth_db"] == pytest.approx(20.0, abs=0.1)
        # And the absolute residual RMS lands where it should
        expected_resid_dbfs = dbfs(rms(ref)) - 20.0
        assert m["rms_residual_dbfs"] == pytest.approx(expected_resid_dbfs, abs=0.1)

    def test_residual_peak_reported(self):
        ref = sine(440.0)
        spike_sig = ref.copy()
        spike_sig[1000, :] += 0.25  # single-sample glitch
        m = measure_residual(ref, spike_sig)
        assert m["peak_residual_dbfs"] == pytest.approx(dbfs(0.25), abs=0.01)

    def test_positive_offset_alignment(self):
        ref = sine(440.0)
        delayed = np.concatenate([np.zeros((100, 2)), ref], axis=0)
        m = measure_residual(ref, delayed, offset=100)
        assert m["null_depth_db"] > 250.0
        assert m["offset"] == 100


class TestFindOffset:
    def test_zero_offset(self):
        ref = sine(440.0) + sine(659.0, amp=0.2)
        assert find_offset(ref, ref.copy()) == 0

    def test_detects_delay(self):
        rng = np.random.default_rng(42)
        ref = rng.standard_normal((SR, 2)) * 0.1
        delayed = np.concatenate([np.zeros((137, 2)), ref], axis=0)
        assert find_offset(ref, delayed) == 137

    def test_detects_advance(self):
        rng = np.random.default_rng(7)
        ref = rng.standard_normal((SR, 2)) * 0.1
        advanced = ref[59:]
        assert find_offset(ref, advanced) == -59


class TestGateA:
    def test_perfect_reconstruction_passes(self):
        ref = sine(440.0)
        g = run_gate_a(ref, ref.copy(), common_gain=0.854274)
        assert g["passed"] is True
        assert g["profile_version"] == PROFILE_VERSION
        assert g["common_gain"] == pytest.approx(0.854274)
        assert g["gate"] == "a_pcm_reconstruction"

    def test_spike_level_residual_passes(self):
        # Spike 0.3 float run: null 22.6 dB, residual peak -10.95 dBFS
        ref = sine(440.0)
        noise = sine(3001.0, amp=1.0)
        noise *= (rms(ref) * 10 ** (-22.6 / 20)) / rms(noise)
        g = run_gate_a(ref, ref + noise, common_gain=0.854274)
        assert g["null_depth_db"] == pytest.approx(22.6, abs=0.1)
        assert g["passed"] is True

    def test_shallow_null_fails(self):
        ref = sine(440.0)
        noise = sine(3001.0, amp=1.0)
        noise *= (rms(ref) * 10 ** (-10 / 20)) / rms(noise)  # only 10 dB null
        g = run_gate_a(ref, ref + noise, common_gain=1.0)
        assert g["passed"] is False

    def test_hot_residual_peak_fails(self):
        # Deep null overall but a residual transient above -6 dBFS
        ref = sine(440.0, amp=0.1)
        bad = ref.copy()
        bad[5000, :] += 0.9  # ~-0.9 dBFS single-sample residual
        g = run_gate_a(ref, bad, common_gain=1.0)
        assert g["peak_residual_dbfs"] > -6.0
        assert g["passed"] is False

    def test_length_mismatch_fails(self):
        ref = sine(440.0)
        g = run_gate_a(ref, ref[:-5], common_gain=1.0)
        assert g["passed"] is False


class TestGateB:
    def test_transparent_roundtrip_passes(self):
        remix = sine(440.0) + sine(659.0, amp=0.2)
        g = run_gate_b(remix, remix.copy(), common_gain=0.854274)
        assert g["passed"] is True
        assert g["offset"] == 0
        assert g["gate"] == "b_decoded_delivery"

    def test_codec_padding_within_tolerance_passes(self):
        remix = sine(440.0)
        decoded = remix[:-1]  # spike 0.4 measured 1 sample short
        g = run_gate_b(remix, decoded, common_gain=1.0)
        assert g["length_delta_samples"] == 1
        assert g["passed"] is True

    def test_length_beyond_tolerance_fails(self):
        remix = sine(440.0)
        decoded = remix[: -(GATE_B_LENGTH_TOLERANCE_SAMPLES + 1)]
        g = run_gate_b(remix, decoded, common_gain=1.0)
        assert g["passed"] is False

    def test_nonzero_offset_fails(self):
        rng = np.random.default_rng(3)
        remix = rng.standard_normal((SR, 2)) * 0.1
        shifted = np.concatenate([np.zeros((64, 2)), remix], axis=0)
        g = run_gate_b(remix, shifted, common_gain=1.0)
        assert g["offset"] == 64
        assert g["passed"] is False

    def test_spike_aac_level_residual_passes(self):
        # Spike 0.4: 320k AAC nulls at ~19.3 dB, residual peak -2.92 dBFS
        remix = sine(440.0)
        noise = sine(3001.0, amp=1.0)
        noise *= (rms(remix) * 10 ** (-19.3 / 20)) / rms(noise)
        g = run_gate_b(remix, remix + noise, common_gain=0.854274)
        assert g["null_depth_db"] == pytest.approx(19.3, abs=0.1)
        assert g["passed"] is True

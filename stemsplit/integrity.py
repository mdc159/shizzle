"""
Pipeline-integrity gates (profile v1) for the Shizzle GPU worker.

These are pipeline-fault detectors, NOT separation-quality scores: even a
perfect pipeline nulls at only ~22.6 dB against the input because htdemucs_6s
stems do not sum exactly to the source (model-inherent, spike 0.3).

Gate (a) — PCM reconstruction, pre-encode:
    unity sum of (float stems x common gain) vs (reference x common gain).
    Pass: null depth >= 15 dB, residual peak <= -6 dBFS, offset == 0,
    sample counts equal.

Gate (b) — decoded-delivery, post-encode:
    unity sum of decoded AAC stems vs the float remix (sum of gained float
    stems). Pass: null depth >= 12 dB, residual peak <= 0 dBFS, best-fit
    offset == 0, decoded length within codec-padding tolerance.

Measurement method (align, unity-sum, RMS/peak residual) lifted from
evidence/spikes/demucs-gain/run.py. Thresholds proposed from one track; re-validate on
~5 tracks before freezing (bump PROFILE_VERSION when thresholds change).
"""

import math

import numpy as np

# ---------------------------------------------------------------- profile v1
PROFILE_VERSION = 1

GATE_A_MIN_NULL_DEPTH_DB = 15.0
GATE_A_MAX_PEAK_RESIDUAL_DBFS = -6.0

GATE_B_MIN_NULL_DEPTH_DB = 12.0
GATE_B_MAX_PEAK_RESIDUAL_DBFS = 0.0
# Decoded AAC may be short/long by codec padding (spike 0.4 measured 1 sample
# short); allow up to two AAC frames (1024 samples each) of length drift.
GATE_B_LENGTH_TOLERANCE_SAMPLES = 2048

# Offset search range for gate (b) alignment (samples).
MAX_OFFSET_SEARCH = 8192

_FLOOR = 1e-15


def dbfs(x: float) -> float:
    """Linear amplitude -> dBFS (floored at -300 dB)."""
    return 20.0 * math.log10(max(float(x), _FLOOR))


def _mono(x: np.ndarray) -> np.ndarray:
    """Downmix (samples, channels) or (samples,) to mono float64."""
    x = np.asarray(x, dtype=np.float64)
    return x.mean(axis=1) if x.ndim == 2 else x


def find_offset(reference: np.ndarray, signal: np.ndarray,
                max_lag: int = MAX_OFFSET_SEARCH) -> int:
    """
    Best-fit lag of `signal` relative to `reference` via FFT cross-correlation
    of the mono downmixes, searched within +/- max_lag samples.

    Positive result means `signal` is delayed by that many samples (i.e.
    signal[offset:] aligns with reference[0:]).
    """
    a = _mono(reference)
    b = _mono(signal)
    n = min(len(a), len(b))
    if n == 0:
        return 0
    a = a[:n]
    b = b[:n]
    size = 1
    while size < 2 * n:
        size *= 2
    fa = np.fft.rfft(a, size)
    fb = np.fft.rfft(b, size)
    corr = np.fft.irfft(fb * np.conj(fa), size)
    # corr[k] = sum b[i] * a[i - k] (circular); lag k in [-max_lag, max_lag]
    max_lag = min(max_lag, n - 1)
    lags = np.concatenate([np.arange(0, max_lag + 1), np.arange(-max_lag, 0)])
    vals = np.concatenate([corr[: max_lag + 1], corr[-max_lag:]])
    return int(lags[int(np.argmax(vals))])


def measure_residual(reference: np.ndarray, signal: np.ndarray,
                     offset: int = 0) -> dict:
    """
    Align `signal` to `reference` by `offset` samples, subtract at unity, and
    measure the residual (spike 0.3 method).

    Returns sample counts, RMS/peak residual in dBFS, and null depth in dB
    (reference RMS dBFS minus residual RMS dBFS).
    """
    ref = np.asarray(reference, dtype=np.float64)
    sig = np.asarray(signal, dtype=np.float64)
    if offset > 0:
        sig = sig[offset:]
    elif offset < 0:
        ref = ref[-offset:]
    n = min(len(ref), len(sig))
    resid = sig[:n] - ref[:n]

    ref_rms = float(np.sqrt(np.mean(ref[:n] ** 2))) if n else 0.0
    resid_rms = float(np.sqrt(np.mean(resid**2))) if n else 0.0
    resid_peak = float(np.max(np.abs(resid))) if n else 0.0

    return {
        "sample_count": n,
        "sample_count_reference": int(len(reference)),
        "sample_count_signal": int(len(signal)),
        "offset": int(offset),
        "reference_rms_dbfs": round(dbfs(ref_rms), 2),
        "rms_residual_dbfs": round(dbfs(resid_rms), 2),
        "peak_residual_dbfs": round(dbfs(resid_peak), 2),
        "null_depth_db": round(dbfs(ref_rms) - dbfs(resid_rms), 2),
    }


def run_gate_a(reference: np.ndarray, stem_sum: np.ndarray,
               common_gain: float) -> dict:
    """
    Gate (a): PCM reconstruction residual before delivery encoding.

    `reference` is the model-rate float decode of the input; `stem_sum` is the
    unity sum of the float stems. Both are compared under the same common
    gain, so the gain cancels out of the null-depth ratio but the recorded
    dBFS figures reflect the delivered levels.
    """
    g = float(common_gain)
    m = measure_residual(np.asarray(reference, dtype=np.float64) * g,
                         np.asarray(stem_sum, dtype=np.float64) * g,
                         offset=0)
    passed = (
        m["null_depth_db"] >= GATE_A_MIN_NULL_DEPTH_DB
        and m["peak_residual_dbfs"] <= GATE_A_MAX_PEAK_RESIDUAL_DBFS
        and m["offset"] == 0
        and m["sample_count_reference"] == m["sample_count_signal"]
    )
    return {
        "gate": "a_pcm_reconstruction",
        "profile_version": PROFILE_VERSION,
        "common_gain": round(g, 6),
        **m,
        "thresholds": {
            "min_null_depth_db": GATE_A_MIN_NULL_DEPTH_DB,
            "max_peak_residual_dbfs": GATE_A_MAX_PEAK_RESIDUAL_DBFS,
        },
        "passed": passed,
    }


def run_gate_b(float_remix: np.ndarray, decoded_sum: np.ndarray,
               common_gain: float) -> dict:
    """
    Gate (b): decoded-delivery residual after AAC encode/decode.

    `float_remix` is the unity sum of the gained float stems (the exact
    player operation on lossless stems); `decoded_sum` is the unity sum of
    the decoded AAC stems. Best-fit offset must be 0 (m4a gapless metadata
    holds) and decoded length within codec-padding tolerance.
    """
    offset = find_offset(float_remix, decoded_sum)
    m = measure_residual(float_remix, decoded_sum, offset=offset)
    length_delta = abs(m["sample_count_signal"] - m["sample_count_reference"])
    passed = (
        m["null_depth_db"] >= GATE_B_MIN_NULL_DEPTH_DB
        and m["peak_residual_dbfs"] <= GATE_B_MAX_PEAK_RESIDUAL_DBFS
        and offset == 0
        and length_delta <= GATE_B_LENGTH_TOLERANCE_SAMPLES
    )
    return {
        "gate": "b_decoded_delivery",
        "profile_version": PROFILE_VERSION,
        "common_gain": round(float(common_gain), 6),
        **m,
        "length_delta_samples": int(length_delta),
        "thresholds": {
            "min_null_depth_db": GATE_B_MIN_NULL_DEPTH_DB,
            "max_peak_residual_dbfs": GATE_B_MAX_PEAK_RESIDUAL_DBFS,
            "max_length_delta_samples": GATE_B_LENGTH_TOLERANCE_SAMPLES,
        },
        "passed": passed,
    }

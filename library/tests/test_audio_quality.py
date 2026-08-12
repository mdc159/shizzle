from __future__ import annotations

import pytest

from shizzle_server.publish.audio_quality import build_six_stem_mix_filter, parse_mix_quality


def test_filter_preserves_per_stem_gain_and_applies_one_master_headroom():
    rendered = build_six_stem_mix_filter([0, -1, -2, -3, -4, -5])
    assert rendered.count("volume=") == 7
    assert "[0:a]volume=0.000000dB[a0]" in rendered
    assert "[5:a]volume=-5.000000dB[a5]" in rendered
    assert "amix=inputs=6" in rendered
    assert "volume=-3.000000dB" in rendered
    assert "ebur128=peak=true" in rendered


def test_filter_requires_exactly_six_stems():
    with pytest.raises(ValueError, match="exactly six"):
        build_six_stem_mix_filter([0] * 5)


def test_parse_quality_summary_and_gate():
    stderr = """
[astats] Overall
[astats] Min level: -0.8
[astats] Max level: 0.7
[astats] Peak level dB: -1.200000
[astats] Number of NaNs: 0.000000
[astats] Number of Infs: 0.000000
[ebur128] Summary:
  Integrated loudness:
    I:         -14.1 LUFS
  Loudness range:
    LRA:         4.2 LU
  True peak:
    Peak:       -1.1 dBFS
"""
    result = parse_mix_quality(stderr)
    assert result.integrated_lufs == -14.1
    assert result.true_peak_dbtp == -1.1
    assert result.passed_pre_limiter
    assert result.additional_common_gain_db == 0


def test_parse_quality_reports_common_gain_for_clipping_mix():
    stderr = """
Overall
Min level: -1.2
Max level: 1.4
Peak level dB: 2.900000
Number of NaNs: 0.000000
Number of Infs: 0.000000
Summary:
  Integrated loudness:
    I:         -8.0 LUFS
  Loudness range:
    LRA:         2.0 LU
  True peak:
    Peak:        3.1 dBFS
"""
    result = parse_mix_quality(stderr)
    assert result.sample_clipping
    assert not result.passed_pre_limiter
    assert result.additional_common_gain_db == -4.1

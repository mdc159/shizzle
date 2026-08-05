"""Tests for the v3 staging manifest contract and the common-gain policy math."""

import numpy as np
import pytest

from audio_processing import STEM_ORDER, compute_common_gain
from config import JobMetadata
from manifest import MANIFEST_VERSION, create_staging_manifest


def make_manifest(**overrides):
    kwargs = {
        "metadata": JobMetadata(title="Song", artist="Band",
                                source_url="https://youtube.com/watch?v=x",
                                video_id="x"),
        "duration": 30.0,
        "sample_rate": 44100,
        "common_gain": {"linear": 0.854274, "db": -1.368,
                        "max_float_stem_peak": 1.14, "max_unity_sum_peak": 1.158},
        "integrity": {"profile_version": 1,
                      "gate_a": {"passed": True}, "gate_b": {"passed": True}},
        "processing": {"model": "htdemucs_6s", "stem_bitrate": "320k"},
    }
    kwargs.update(overrides)
    return create_staging_manifest(**kwargs)


class TestManifest:
    def test_version_is_3(self):
        assert make_manifest()["version"] == MANIFEST_VERSION == 3

    def test_default_gain_db_semantics(self):
        m = make_manifest()
        for stem in m["stems"]:
            assert "default_gain" not in stem  # the linear bug must not return
            assert stem["default_gain_db"] == 0.0

    def test_stem_ids_and_files(self):
        m = make_manifest()
        assert len(m["stems"]) == 6
        by_file = {s["file"]: s for s in m["stems"]}
        assert by_file["stems/other.m4a"]["id"] == "shizzle"
        assert by_file["stems/other.m4a"]["name"] == "Shizzle"
        assert by_file["stems/vocals.m4a"]["id"] == "vocals"
        assert [s["file"] for s in m["stems"]] == [
            f"stems/{name}.m4a" for name in STEM_ORDER
        ]

    def test_common_gain_and_integrity_recorded(self):
        m = make_manifest()
        assert m["common_gain"]["linear"] == pytest.approx(0.854274)
        assert m["integrity"]["profile_version"] == 1
        assert m["integrity"]["gate_a"]["passed"] is True

    def test_timeline(self):
        m = make_manifest()
        assert m["timeline"] == {
            "start_ms": 0,
            "duration_ms": 30000,
            "sample_rate_hz": 44100,
        }

    def test_multitrack_flag(self):
        assert "multitrack" not in make_manifest()
        assert make_manifest(multitrack=True)["multitrack"] == "multi-track.mp4"


class TestCommonGain:
    def test_attenuates_to_target_from_sum_peak(self):
        # Two stems each peaking 0.6 -> unity sum peaks 1.2 > 0.99
        x = np.zeros((1000, 2), dtype=np.float32)
        x[500] = 0.6
        stems = {"a": x, "b": x.copy()}
        g = compute_common_gain(stems)
        assert g["linear"] == pytest.approx(0.99 / 1.2, abs=1e-6)
        assert g["max_unity_sum_peak"] == pytest.approx(1.2, abs=1e-6)
        assert g["max_float_stem_peak"] == pytest.approx(0.6, abs=1e-6)

    def test_attenuates_from_stem_peak_when_larger(self):
        # One hot stem (1.15, spike-style drums overshoot), one out of phase
        a = np.zeros((1000, 2), dtype=np.float32)
        a[500] = 1.15
        b = np.zeros((1000, 2), dtype=np.float32)
        b[500] = -0.5
        g = compute_common_gain({"a": a, "b": b})
        assert g["linear"] == pytest.approx(0.99 / 1.15, abs=1e-6)

    def test_never_amplifies(self):
        x = np.full((100, 2), 0.01, dtype=np.float32)
        g = compute_common_gain({"a": x})
        assert g["linear"] == 1.0
        assert g["db"] == 0.0

    def test_common_gain_preserves_relative_levels(self):
        rng = np.random.default_rng(1)
        a = rng.standard_normal((4410, 2)).astype(np.float32)
        b = (rng.standard_normal((4410, 2)) * 0.3).astype(np.float32)
        g = compute_common_gain({"a": a, "b": b})["linear"]
        ratio_before = np.abs(a).max() / np.abs(b).max()
        ratio_after = np.abs(a * g).max() / np.abs(b * g).max()
        assert ratio_after == pytest.approx(ratio_before, rel=1e-6)

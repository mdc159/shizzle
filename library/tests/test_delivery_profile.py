from __future__ import annotations

from shizzle_server.delivery_profile import (
    CANONICAL_STEM_IDS,
    PROFILE_ID,
    canonical_stem_id,
    evaluate_audio_probe,
    evaluate_manifest,
    evaluate_video_probe,
    has_errors,
    profile_manifest_block,
)


def _manifest() -> dict:
    return {
        "delivery_profile": PROFILE_ID,
        "video": "video.mp4",
        "stems": [
            {
                "id": role,
                "file": f"stems/{role}.m4a",
                "default_gain_db": 0.0,
            }
            for role in CANONICAL_STEM_IDS
        ],
    }


def test_manifest_requires_six_canonical_roles_and_paths():
    assert evaluate_manifest(_manifest()) == []
    broken = _manifest()
    broken["stems"][-1]["id"] = "other"
    broken["stems"][-1]["file"] = "stems/other.m4a"
    issues = evaluate_manifest(broken)
    assert {issue.code for issue in issues} == {"manifest-stem-path-noncanonical"}
    assert canonical_stem_id("other") == "shizzle"


def test_manifest_detects_missing_duplicate_role_and_gain_units():
    broken = _manifest()
    broken["stems"].pop()
    broken["stems"][0]["id"] = "drums"
    broken["stems"][0].pop("default_gain_db")
    codes = {issue.code for issue in evaluate_manifest(broken)}
    assert {
        "manifest-stem-count",
        "manifest-roles-missing",
        "manifest-roles-duplicated",
        "manifest-default-gain-missing",
    } <= codes


def test_passing_audio_probe():
    issues = evaluate_audio_probe(
        {
            "codec_name": "aac",
            "profile": "LC",
            "channels": 2,
            "sample_rate": "44100",
            "start_time": "0.000000",
            "duration": "378.648005",
            "bit_rate": "249324",
        },
        {"duration": "378.648005"},
        artifact="stems/vocals.m4a",
        expected_duration=378.648005,
        fast_start=True,
        preserve_existing_lossy=True,
    )
    assert issues == []


def test_existing_low_bitrate_is_warning_but_new_encode_is_error():
    stream = {
        "codec_name": "aac",
        "profile": "LC",
        "channels": 2,
        "sample_rate": "44100",
        "start_time": "0",
        "duration": "10",
        "bit_rate": "180000",
    }
    legacy = evaluate_audio_probe(
        stream,
        {},
        artifact="stems/bass.m4a",
        expected_duration=10,
        fast_start=True,
        preserve_existing_lossy=True,
    )
    derived = evaluate_audio_probe(
        stream,
        {},
        artifact="stems/bass.m4a",
        expected_duration=10,
        fast_start=True,
        preserve_existing_lossy=False,
    )
    assert [issue.severity for issue in legacy if issue.code == "audio-bitrate-low"] == ["warning"]
    assert [issue.severity for issue in derived if issue.code == "audio-bitrate-low"] == ["error"]
    assert not has_errors(legacy)
    assert has_errors(derived)


def test_existing_48khz_is_preserved_but_new_derivation_uses_44k1():
    stream = {
        "codec_name": "aac",
        "profile": "LC",
        "channels": 2,
        "sample_rate": "48000",
        "start_time": "0",
        "duration": "10.066",
        "bit_rate": "192000",
    }
    legacy = evaluate_audio_probe(
        stream,
        {},
        artifact="stems/drums.m4a",
        expected_duration=10,
        fast_start=True,
        preserve_existing_lossy=True,
    )
    derived = evaluate_audio_probe(
        stream,
        {},
        artifact="stems/drums.m4a",
        expected_duration=10,
        fast_start=True,
        preserve_existing_lossy=False,
    )
    assert not has_errors(legacy)
    assert {issue.code for issue in derived} == {"audio-sample-rate"}


def test_passing_video_probe_matches_repaired_pot():
    issues = evaluate_video_probe(
        {
            "codec_name": "h264",
            "profile": "Main",
            "pix_fmt": "yuv420p",
            "avg_frame_rate": "30/1",
            "start_time": "0.000000",
            "duration": "378.633333",
        },
        {"duration": "378.633333"},
        artifact="video.mp4",
        expected_duration=378.648005,
        fast_start=True,
        max_keyframe_interval_sec=2.0,
        audio_stream_count=0,
    )
    assert issues == []


def test_broken_legacy_video_shape_is_release_blocking():
    issues = evaluate_video_probe(
        {
            "codec_name": "h264",
            "profile": "Baseline",
            "pix_fmt": "yuv420p",
            "avg_frame_rate": "30/1",
            "start_time": "0",
            "duration": "378.63",
        },
        {"duration": "378.63"},
        artifact="video.mp4",
        expected_duration=378.648,
        fast_start=False,
        max_keyframe_interval_sec=7.0,
        audio_stream_count=1,
    )
    codes = {issue.code for issue in issues}
    assert {
        "video-profile",
        "video-has-audio",
        "video-not-fast-start",
        "video-keyframe-gap",
    } <= codes
    assert has_errors(issues)


def test_compatible_source_frame_rates_and_high_profile_are_preserved():
    issues = evaluate_video_probe(
        {
            "codec_name": "h264",
            "profile": "High",
            "pix_fmt": "yuv420p",
            "avg_frame_rate": "30000/1001",
            "start_time": "0",
            "duration": "10",
        },
        {"duration": "10"},
        artifact="video.mp4",
        expected_duration=10,
        fast_start=True,
        max_keyframe_interval_sec=2.002,
        audio_stream_count=0,
    )
    assert issues == []


def test_profile_manifest_block_is_versioned_and_unit_bearing():
    block = profile_manifest_block()
    assert block["id"] == PROFILE_ID
    assert block["audio"]["sample_rate_hz"] == 44_100
    assert block["audio"]["target_bitrate_bps"] == 256_000
    assert block["video"]["max_keyframe_interval_seconds"] == 2.05

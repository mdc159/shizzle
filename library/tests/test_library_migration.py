from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from shizzle_server.publish.delivery_profile import CANONICAL_STEM_IDS, PROFILE_ID
from shizzle_server.publish.library_migration import (
    build_migrated_manifest,
    decide_migration,
    video_encode_command,
)


def _audit(total: int = 2_000_000, *, video_passed: bool = True) -> dict:
    return {
        "total_average_bitrate_bps": total,
        "objects": [
            {"artifact": "video.mp4", "passed": video_passed},
            *[
                {
                    "artifact": f"stems/{'other' if role == 'shizzle' else role}.m4a",
                    "passed": True,
                }
                for role in CANONICAL_STEM_IDS
            ],
        ],
    }


def test_decision_copies_compatible_video_and_never_reencodes_aac():
    decision = decide_migration(_audit())
    assert decision.video_action == "copy"
    assert decision.stem_actions == dict.fromkeys(CANONICAL_STEM_IDS, "copy")


def test_decision_encodes_over_budget_or_failed_video():
    assert decide_migration(_audit(3_000_000)).video_action == "encode"
    assert decide_migration(_audit(video_passed=False)).video_action == "encode"


def test_decision_blocks_failed_lossy_stem():
    audit = _audit()
    audit["objects"][2]["passed"] = False
    with pytest.raises(ValueError, match="lossless recovery"):
        decide_migration(audit)


def test_manifest_canonicalizes_other_and_records_provenance():
    source = {
        "version": 3,
        "title": "Example",
        "video": "legacy.mp4",
        "stems": [
            {
                "id": "other" if role == "shizzle" else role,
                "name": role.title(),
                "file": f"stems/{'other' if role == 'shizzle' else role}.m4a",
                "default_gain_db": -1.0,
            }
            for role in CANONICAL_STEM_IDS
        ],
        "integrity": {"source": "legacy-import"},
    }
    decision = decide_migration(_audit())
    manifest = build_migrated_manifest(
        source,
        track_id="track-id",
        source_generation=1,
        generation=2,
        audit_sha256="abc",
        decision=decision,
        object_provenance=[{"file": "video.mp4", "sha256": "123"}],
    )
    assert manifest["delivery_profile"] == PROFILE_ID
    assert [stem["id"] for stem in manifest["stems"]] == list(CANONICAL_STEM_IDS)
    assert manifest["stems"][-1]["file"] == "stems/shizzle.m4a"
    assert {stem["default_gain_db"] for stem in manifest["stems"]} == {-1.0}
    assert manifest["integrity"]["source_generation"] == 1
    assert manifest["integrity"]["previous_integrity"] == {"source": "legacy-import"}


def test_manifest_applies_one_measured_common_gain_to_every_stem():
    source = {
        "version": 3,
        "stems": [
            {
                "id": role,
                "name": role.title(),
                "file": f"stems/{role}.m4a",
                "default_gain_db": 0.0,
            }
            for role in CANONICAL_STEM_IDS
        ],
    }
    reason = "decoded mix measured -0.8 dBTP; target <= -1.0 dBTP"
    manifest = build_migrated_manifest(
        source,
        track_id="track-id",
        source_generation=2,
        generation=3,
        audit_sha256="abc",
        decision=decide_migration(_audit()),
        object_provenance=[],
        common_gain_db=-0.2,
        common_gain_reason=reason,
    )

    assert {stem["default_gain_db"] for stem in manifest["stems"]} == {-0.2}
    assert manifest["integrity"]["common_gain_db"] == -0.2
    assert manifest["integrity"]["common_gain_reason"] == reason


def test_video_encode_command_is_bounded_seekable_and_audio_less(tmp_path: Path):
    command = video_encode_command(tmp_path / "source.mp4", tmp_path / "video.mp4")
    rendered = " ".join(command)
    assert "-an" in command
    assert "fps=30" in rendered
    assert "-g 60" in rendered
    assert "+faststart" in command
    assert "-maxrate 1200k" in rendered
    assert "min(720,ih)" in rendered
    assert "force_divisible_by=2" in rendered


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required"
)
def test_video_encode_command_makes_nonstandard_source_dimensions_even(tmp_path: Path):
    source = tmp_path / "source.mp4"
    destination = tmp_path / "video.mp4"
    generated = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1460x1080:d=0.1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert generated.returncode == 0, generated.stderr
    encoded = subprocess.run(
        video_encode_command(source, destination),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert encoded.returncode == 0, encoded.stderr
    probed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(destination),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    stream = json.loads(probed.stdout)["streams"][0]
    assert 0 < stream["width"] <= 1280
    assert 0 < stream["height"] <= 720
    assert stream["width"] % 2 == 0
    assert stream["height"] % 2 == 0

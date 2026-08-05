"""Pure planning helpers for immutable legacy-library delivery migration."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .delivery_profile import (
    MAX_TOTAL_AVERAGE_BITRATE,
    PROFILE_ID,
    canonical_stem_id,
    expected_stem_file,
    profile_manifest_block,
)


@dataclass(frozen=True)
class MigrationDecision:
    video_action: Literal["copy", "encode"]
    stem_actions: dict[str, Literal["copy"]]
    reason: str


def decide_migration(audit: dict[str, Any]) -> MigrationDecision:
    """Choose lossless actions from measured artifact evidence.

    Existing AAC is never re-encoded. A failing stem therefore blocks instead
    of being silently made worse; it must be recovered from lossless material.
    """
    objects = audit.get("objects", [])
    video = next((item for item in objects if item.get("artifact") == "video.mp4"), None)
    stems = [item for item in objects if item.get("artifact") != "video.mp4"]
    if video is None or len(stems) != 6:
        raise ValueError("audit must contain one video and six stems")
    failed_stems = [item["artifact"] for item in stems if not item.get("passed")]
    if failed_stems:
        raise ValueError(
            "lossy stems failed delivery gates and require lossless recovery: "
            + ", ".join(failed_stems)
        )
    total = int(audit.get("total_average_bitrate_bps") or 0)
    video_action: Literal["copy", "encode"] = (
        "encode" if not video.get("passed") or total > MAX_TOTAL_AVERAGE_BITRATE else "copy"
    )
    reason = (
        f"total bitrate {total} exceeds {MAX_TOTAL_AVERAGE_BITRATE}"
        if total > MAX_TOTAL_AVERAGE_BITRATE
        else "existing video passes compatibility profile and bitrate budget"
    )
    if not video.get("passed"):
        reason = "existing video fails delivery artifact gates"
    stem_actions = {canonical_stem_id(Path(item["artifact"]).stem): "copy" for item in stems}
    return MigrationDecision(video_action=video_action, stem_actions=stem_actions, reason=reason)


def build_migrated_manifest(
    source: dict[str, Any],
    *,
    track_id: str,
    source_generation: int,
    generation: int,
    audit_sha256: str,
    decision: MigrationDecision,
    object_provenance: list[dict[str, Any]],
    common_gain_db: float | None = None,
    common_gain_reason: str | None = None,
) -> dict[str, Any]:
    manifest = copy.deepcopy(source)
    source_stems = {
        canonical_stem_id(str(stem.get("id", ""))): stem
        for stem in source.get("stems", [])
        if isinstance(stem, dict)
    }
    manifest["track_id"] = track_id
    manifest["generation"] = generation
    manifest["delivery_profile"] = PROFILE_ID
    manifest["delivery_profile_details"] = profile_manifest_block()
    manifest["video"] = "video.mp4"
    manifest["stems"] = [
        {
            "id": role,
            "name": source_stems.get(role, {}).get("name", role.title()),
            "file": expected_stem_file(role),
            "default_gain_db": (
                float(common_gain_db)
                if common_gain_db is not None
                else float(source_stems.get(role, {}).get("default_gain_db", 0.0))
            ),
        }
        for role in decision.stem_actions
    ]
    manifest["integrity"] = {
        "source": "audited-library-migration",
        "profile": PROFILE_ID,
        "audit_sha256": audit_sha256,
        "source_generation": source_generation,
        "video_action": decision.video_action,
        "video_action_reason": decision.reason,
        "objects": object_provenance,
        "previous_integrity": source.get("integrity"),
    }
    if common_gain_db is not None:
        manifest["integrity"]["common_gain_db"] = float(common_gain_db)
        manifest["integrity"]["common_gain_reason"] = common_gain_reason or "measured headroom"
    return manifest


def video_encode_command(source: Path, destination: Path) -> list[str]:
    """The bounded 720p/30 browser derivative used for over-budget video."""
    return [
        "ffmpeg",
        "-nostdin",
        "-y",
        "-fflags",
        "+genpts",
        "-i",
        str(source),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        "scale=w='min(1280,iw)':h='min(720,ih)':"
        "force_original_aspect_ratio=decrease:force_divisible_by=2,"
        "fps=30,format=yuv420p,setpts=PTS-STARTPTS",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-maxrate",
        "1200k",
        "-bufsize",
        "2400k",
        "-profile:v",
        "main",
        "-level:v",
        "3.1",
        "-g",
        "60",
        "-keyint_min",
        "60",
        "-sc_threshold",
        "0",
        "-movflags",
        "+faststart",
        "-video_track_timescale",
        "90000",
        str(destination),
    ]

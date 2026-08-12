"""Executable browser-delivery contract for immutable Shizzle media generations.

This module deliberately contains no boto3, ffmpeg, or database code.  The
worker, VPS audit/migration commands, publisher, and tests consume the same
pure policy instead of carrying similar-looking command-line constants in
several places.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Any, Literal

PROFILE_ID = "shizzle-browser-v1"
PROFILE_VERSION = 1

CANONICAL_STEM_IDS = ("vocals", "drums", "bass", "guitar", "piano", "shizzle")
LEGACY_STEM_ALIASES = {"other": "shizzle"}

AUDIO_CODEC = "aac"
AUDIO_PROFILE = "LC"
AUDIO_CONTAINER_SUFFIX = ".m4a"
AUDIO_CHANNELS = 2
AUDIO_SAMPLE_RATE = 44_100
AUDIO_COMPATIBLE_SAMPLE_RATES = (44_100, 48_000)
AUDIO_TARGET_BITRATE = 256_000
AUDIO_MIN_BITRATE = 192_000

VIDEO_CODEC = "h264"
VIDEO_PROFILE = "Main"
VIDEO_COMPATIBLE_PROFILES = ("Main", "High")
VIDEO_PIXEL_FORMAT = "yuv420p"
VIDEO_FRAME_RATE = Fraction(30, 1)
VIDEO_COMPATIBLE_FRAME_RATES = (
    Fraction(24_000, 1_001),
    Fraction(24, 1),
    Fraction(25, 1),
    Fraction(30_000, 1_001),
    Fraction(30, 1),
    Fraction(60_000, 1_001),
)
VIDEO_MAX_KEYFRAME_INTERVAL_SEC = 2.05

START_TOLERANCE_SEC = 0.020
# AAC priming/padding in the measured legacy library produces a common
# six-stem end overhang of up to 66 ms. Identical stem timelines are the hard
# invariant; a bounded common tail beyond the video master is harmless.
STEM_DURATION_TOLERANCE_SEC = 0.080
STEM_INTER_DURATION_TOLERANCE_SEC = 0.005
# The video-duration gate compares an H.264 encode against the stem timeline.
# At 30 fps one frame is ~33 ms and a clean encode can end a single frame early
# or late; 50 ms barely cleared one frame and tripped on GOP quantization, so
# the tolerance is at least one full frame-duration of headroom (wave3 #5).
# Used by derive_video's post-encode probe and the delivery profile stream
# check — both are the same invariant (video vs stem timeline).
TRACK_DURATION_TOLERANCE_SEC = 0.100
MAX_TOTAL_AVERAGE_BITRATE = 2_500_000


@dataclass(frozen=True)
class ProfileIssue:
    """One machine-readable delivery-contract violation."""

    code: str
    message: str
    artifact: str | None = None
    severity: Literal["error", "warning"] = "error"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def canonical_stem_id(stem_id: str) -> str:
    """Map a known legacy role to the public v1 role name."""
    lowered = stem_id.strip().lower()
    return LEGACY_STEM_ALIASES.get(lowered, lowered)


def expected_stem_file(stem_id: str) -> str:
    """Return the canonical immutable-generation path for a stem role."""
    return f"stems/{canonical_stem_id(stem_id)}{AUDIO_CONTAINER_SUFFIX}"


def _number(value: Any) -> float | None:
    if value in (None, "", "N/A"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _fraction(value: Any) -> Fraction | None:
    if value in (None, "", "N/A", "0/0"):
        return None
    try:
        return Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None


def evaluate_manifest(manifest: dict[str, Any]) -> list[ProfileIssue]:
    """Validate the playback-visible shape independently of media probing."""
    issues: list[ProfileIssue] = []
    stems = manifest.get("stems")
    if not isinstance(stems, list):
        return [ProfileIssue("manifest-stems-missing", "manifest.stems must be a list")]

    seen: list[str] = []
    for index, stem in enumerate(stems):
        artifact = f"stems[{index}]"
        if not isinstance(stem, dict):
            issues.append(ProfileIssue("manifest-stem-invalid", "stem must be an object", artifact))
            continue
        role = canonical_stem_id(str(stem.get("id", "")))
        seen.append(role)
        if role not in CANONICAL_STEM_IDS:
            issues.append(ProfileIssue("manifest-role-unknown", f"unknown stem role {role!r}", artifact))
        path = str(stem.get("file", ""))
        expected = expected_stem_file(role)
        if path != expected:
            issues.append(
                ProfileIssue(
                    "manifest-stem-path-noncanonical",
                    f"{role or '(missing role)'} must use {expected}, got {path or '(missing)'}",
                    artifact,
                )
            )
        if not isinstance(stem.get("default_gain_db"), int | float):
            issues.append(
                ProfileIssue(
                    "manifest-default-gain-missing",
                    "default_gain_db must be a numeric dB value",
                    artifact,
                )
            )

    if len(seen) != len(CANONICAL_STEM_IDS):
        issues.append(
            ProfileIssue(
                "manifest-stem-count",
                f"expected {len(CANONICAL_STEM_IDS)} stems, got {len(seen)}",
            )
        )
    missing = sorted(set(CANONICAL_STEM_IDS) - set(seen))
    duplicates = sorted(role for role in set(seen) if seen.count(role) > 1)
    if missing:
        issues.append(ProfileIssue("manifest-roles-missing", f"missing roles: {', '.join(missing)}"))
    if duplicates:
        issues.append(
            ProfileIssue("manifest-roles-duplicated", f"duplicated roles: {', '.join(duplicates)}")
        )

    if manifest.get("video") != "video.mp4":
        issues.append(ProfileIssue("manifest-video-path", "video must be video.mp4", "video"))
    if manifest.get("delivery_profile") != PROFILE_ID:
        issues.append(
            ProfileIssue(
                "manifest-profile",
                f"delivery_profile must be {PROFILE_ID}",
                severity="warning",
            )
        )
    return issues


def evaluate_audio_probe(
    stream: dict[str, Any],
    format_info: dict[str, Any],
    *,
    artifact: str,
    expected_duration: float,
    fast_start: bool,
    preserve_existing_lossy: bool,
) -> list[ProfileIssue]:
    """Evaluate one ffprobe audio stream against the v1 browser profile."""
    issues: list[ProfileIssue] = []
    if stream.get("codec_name") != AUDIO_CODEC:
        issues.append(ProfileIssue("audio-codec", f"expected AAC, got {stream.get('codec_name')}", artifact))
    profile = str(stream.get("profile", ""))
    if profile and profile != AUDIO_PROFILE:
        issues.append(ProfileIssue("audio-profile", f"expected AAC-LC, got {profile}", artifact))
    if _integer(stream.get("channels")) != AUDIO_CHANNELS:
        issues.append(
            ProfileIssue("audio-channels", f"expected stereo, got {stream.get('channels')}", artifact)
        )
    sample_rate = _integer(stream.get("sample_rate"))
    allowed_rates = AUDIO_COMPATIBLE_SAMPLE_RATES if preserve_existing_lossy else (AUDIO_SAMPLE_RATE,)
    if sample_rate not in allowed_rates:
        issues.append(
            ProfileIssue(
                "audio-sample-rate",
                f"expected one of {allowed_rates} Hz, got {stream.get('sample_rate')}",
                artifact,
            )
        )

    start = _number(stream.get("start_time"))
    if start is None:
        start = _number(format_info.get("start_time"))
    if start is None or abs(start) > START_TOLERANCE_SEC:
        issues.append(ProfileIssue("audio-start", f"start time {start!r} is not zero-based", artifact))

    duration = _number(stream.get("duration"))
    if duration is None:
        duration = _number(format_info.get("duration"))
    if duration is None or abs(duration - expected_duration) > STEM_DURATION_TOLERANCE_SEC:
        issues.append(
            ProfileIssue(
                "audio-duration",
                f"duration {duration!r} differs from {expected_duration:.6f}s by more than "
                f"{STEM_DURATION_TOLERANCE_SEC:.3f}s",
                artifact,
            )
        )

    bitrate = _integer(stream.get("bit_rate")) or _integer(format_info.get("bit_rate"))
    if bitrate is not None and bitrate < AUDIO_MIN_BITRATE:
        issues.append(
            ProfileIssue(
                "audio-bitrate-low",
                f"average bitrate {bitrate} b/s is below {AUDIO_MIN_BITRATE} b/s",
                artifact,
                severity="warning" if preserve_existing_lossy else "error",
            )
        )
    if not fast_start:
        issues.append(ProfileIssue("audio-not-fast-start", "moov atom does not precede mdat", artifact))
    return issues


def evaluate_video_probe(
    stream: dict[str, Any],
    format_info: dict[str, Any],
    *,
    artifact: str,
    expected_duration: float,
    fast_start: bool,
    max_keyframe_interval_sec: float | None,
    audio_stream_count: int,
) -> list[ProfileIssue]:
    """Evaluate one ffprobe video stream and derived keyframe evidence."""
    issues: list[ProfileIssue] = []
    if stream.get("codec_name") != VIDEO_CODEC:
        issues.append(ProfileIssue("video-codec", f"expected H.264, got {stream.get('codec_name')}", artifact))
    profile = stream.get("profile")
    if profile not in VIDEO_COMPATIBLE_PROFILES:
        issues.append(
            ProfileIssue(
                "video-profile",
                f"expected one of {VIDEO_COMPATIBLE_PROFILES}, got {profile}",
                artifact,
            )
        )
    if stream.get("pix_fmt") != VIDEO_PIXEL_FORMAT:
        issues.append(
            ProfileIssue(
                "video-pixel-format",
                f"expected {VIDEO_PIXEL_FORMAT}, got {stream.get('pix_fmt')}",
                artifact,
            )
        )
    frame_rate = _fraction(stream.get("avg_frame_rate"))
    if frame_rate is None or all(
        abs(float(frame_rate - allowed)) > 0.01 for allowed in VIDEO_COMPATIBLE_FRAME_RATES
    ):
        issues.append(
            ProfileIssue(
                "video-frame-rate",
                f"unsupported frame rate {stream.get('avg_frame_rate')}",
                artifact,
            )
        )
    start = _number(stream.get("start_time"))
    if start is None:
        start = _number(format_info.get("start_time"))
    if start is None or abs(start) > START_TOLERANCE_SEC:
        issues.append(ProfileIssue("video-start", f"start time {start!r} is not zero-based", artifact))
    duration = _number(stream.get("duration"))
    if duration is None:
        duration = _number(format_info.get("duration"))
    if duration is None or abs(duration - expected_duration) > TRACK_DURATION_TOLERANCE_SEC:
        issues.append(
            ProfileIssue(
                "video-duration",
                f"duration {duration!r} differs from {expected_duration:.6f}s by more than "
                f"{TRACK_DURATION_TOLERANCE_SEC:.3f}s",
                artifact,
            )
        )
    if audio_stream_count:
        issues.append(
            ProfileIssue("video-has-audio", f"delivery video contains {audio_stream_count} audio stream(s)", artifact)
        )
    if not fast_start:
        issues.append(ProfileIssue("video-not-fast-start", "moov atom does not precede mdat", artifact))
    if max_keyframe_interval_sec is None:
        issues.append(ProfileIssue("video-keyframes-unmeasured", "keyframe spacing was not measured", artifact))
    elif max_keyframe_interval_sec > VIDEO_MAX_KEYFRAME_INTERVAL_SEC:
        issues.append(
            ProfileIssue(
                "video-keyframe-gap",
                f"maximum keyframe interval {max_keyframe_interval_sec:.3f}s exceeds "
                f"{VIDEO_MAX_KEYFRAME_INTERVAL_SEC:.3f}s",
                artifact,
            )
        )
    return issues


def has_errors(issues: list[ProfileIssue]) -> bool:
    """Return true when at least one release-blocking issue exists."""
    return any(issue.severity == "error" for issue in issues)


def profile_manifest_block() -> dict[str, Any]:
    """Stable profile metadata embedded in every newly derived manifest."""
    return {
        "id": PROFILE_ID,
        "version": PROFILE_VERSION,
        "audio": {
            "codec": "AAC-LC",
            "container": "M4A",
            "channels": AUDIO_CHANNELS,
            "sample_rate_hz": AUDIO_SAMPLE_RATE,
            "target_bitrate_bps": AUDIO_TARGET_BITRATE,
            "minimum_bitrate_bps": AUDIO_MIN_BITRATE,
        },
        "video": {
            "codec": "H.264",
            "profile": VIDEO_PROFILE,
            "pixel_format": VIDEO_PIXEL_FORMAT,
            "frame_rate": float(VIDEO_FRAME_RATE),
            "max_keyframe_interval_seconds": VIDEO_MAX_KEYFRAME_INTERVAL_SEC,
        },
    }

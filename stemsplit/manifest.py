"""
Staging manifest generation for the Shizzle GPU worker.

Emits a v3-compatible manifest (schema documented in MANIFEST.md):
  - stem field `default_gain_db` (dB semantics; the unit-bearing name kills
    the latent linear-0-is-silence bug from the v2 `default_gain` field)
  - common_gain block (the one gain baked into every stem, spike 0.3 policy)
  - integrity results for both gates (profile v1)

The publisher copies this staging manifest into the immutable generation
prefix LAST, after verifying staged objects.
"""

import json
from pathlib import Path
from typing import Any

from audio_processing import STEM_DISPLAY_NAMES, STEM_ID_MAP, STEM_ORDER

MANIFEST_VERSION = 3


def create_staging_manifest(
    metadata: Any,
    duration: float,
    sample_rate: int,
    common_gain: dict[str, float],
    integrity: dict[str, Any],
    processing: dict[str, Any],
    video_meta: dict[str, Any] | None = None,
    multitrack: bool = False,
) -> dict[str, Any]:
    """
    Create the staging manifest for a processed track.

    Args:
        metadata: JobMetadata (title, artist, source_url, video_id)
        duration: media duration in seconds
        sample_rate: stem sample rate in Hz (model rate, 44100 for htdemucs_6s)
        common_gain: dict from audio_processing.compute_common_gain
        integrity: {"profile_version": int, "gate_a": {...}, "gate_b": {...}}
        processing: model/parameters/codec provenance block
        video_meta: optional ffprobe diagnostics for video.mp4
        multitrack: whether multi-track.mp4 was produced

    Returns:
        Manifest dict ready for JSON serialization.
    """
    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "title": metadata.title,
        "artist": metadata.artist,
        "duration": duration,
        "video": "video.mp4",
        "sourceUrl": metadata.source_url,
        "videoId": metadata.video_id,
        "timeline": {
            "start_ms": 0,
            "duration_ms": int(round(duration * 1000)),
            "sample_rate_hz": sample_rate,
        },
        "video_meta": video_meta or {},
        "stems": [
            {
                "id": STEM_ID_MAP[stem],
                "name": STEM_DISPLAY_NAMES[stem],
                "file": f"stems/{stem}.m4a",
                # dB, not linear. Consumers convert via dbToLinear() at load
                # time. 0.0 because the common gain is already baked into the
                # encoded stems.
                "default_gain_db": 0.0,
            }
            for stem in STEM_ORDER
        ],
        "common_gain": common_gain,
        "integrity": integrity,
        "processing": processing,
    }
    if multitrack:
        manifest["multitrack"] = "multi-track.mp4"
    return manifest


def write_manifest(manifest: dict[str, Any], output_path: Path) -> None:
    """Write manifest to JSON file."""
    output_path.write_text(json.dumps(manifest, indent=2))
    print(f"Created manifest: {output_path}", flush=True)

"""
Manifest generation for Runpod Demucs handler.

Creates staging manifest.json per s3-naming-spec.md.
"""

import json
from pathlib import Path
from typing import Any

from audio_processing import STEM_ORDER


def create_staging_manifest(
    metadata: Any,
    duration: float,
    stem_files: dict[str, Path],
) -> dict[str, Any]:
    """
    Create staging manifest.json for processed karaoke job.

    References WAV files in stems/ subfolder - publish step converts to m4a.

    Args:
        metadata: JobMetadata with title, artist, etc.
        duration: Video duration in seconds
        stem_files: Dict mapping stem name to WAV path

    Returns:
        Manifest dict ready for JSON serialization
    """
    return {
        "title": metadata.title,
        "artist": metadata.artist,
        "duration": duration,
        "video": "video.mp4",
        "sourceUrl": metadata.source_url,
        "videoId": metadata.video_id,
        "stems": [
            {
                "id": stem,
                "name": stem,
                "file": f"stems/{stem}.wav",
                "default_gain": 1.0,
            }
            for stem in STEM_ORDER
            if stem in stem_files
        ],
    }


def write_manifest(manifest: dict[str, Any], output_path: Path) -> None:
    """
    Write manifest to JSON file.

    Args:
        manifest: Manifest dict
        output_path: Path to write manifest.json
    """
    output_path.write_text(json.dumps(manifest, indent=2))
    print(f"Created manifest: {output_path}", flush=True)

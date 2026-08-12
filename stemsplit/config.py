"""
Configuration and input validation for the Shizzle GPU worker.

Validates job inputs and provides typed configuration. Staging semantics:
outputs land under tracks/{track_id}/{generation}/staging/ and the publisher
(VPS) later verifies + promotes them into the immutable generation prefix.
"""

import os
import re
from dataclasses import dataclass, field
from typing import Any

# S3 path validation patterns (staging layout per design spec section 3).
# track_id / generation: alphanumeric, underscores, hyphens.
ALLOWED_INPUT_PATTERN = re.compile(
    r"^tracks/[a-zA-Z0-9_-]+/[a-zA-Z0-9_-]+/staging/source\.mp4$"
)
ALLOWED_OUTPUT_PATTERN = re.compile(
    r"^tracks/(?P<track_id>[a-zA-Z0-9_-]+)/(?P<generation>[a-zA-Z0-9_-]+)/staging/$"
)

BITRATE_PATTERN = re.compile(r"^\d{2,4}k$")

# Browser delivery profile target. Existing passing AAC is preserved; this
# default applies only to new encodes derived from lossless stems.
DEFAULT_AAC_BITRATE = "256k"


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean env var ("true"/"1"/"yes" vs "false"/"0"/"no")."""
    v = os.getenv(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def stem_aac_bitrate() -> str:
    """The AAC stem bitrate knob (STEM_AAC_BITRATE env, default 256k)."""
    v = os.getenv("STEM_AAC_BITRATE", DEFAULT_AAC_BITRATE).strip()
    if not BITRATE_PATTERN.match(v):
        raise InputValidationError(f"Invalid STEM_AAC_BITRATE: {v!r} (expected e.g. '320k')")
    return v


def create_multitrack_default() -> bool:
    """CREATE_MULTITRACK_MP4 env flag (default true — Mike's archival format)."""
    return _env_flag("CREATE_MULTITRACK_MP4", True)


@dataclass
class JobMetadata:
    """Metadata for the karaoke job."""
    title: str = "Unknown"
    artist: str = "Unknown"
    source_url: str = ""
    video_id: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "JobMetadata":
        return cls(
            title=data.get("title", "Unknown"),
            artist=data.get("artist", "Unknown"),
            source_url=data.get("sourceUrl", ""),
            video_id=data.get("videoId", ""),
        )


@dataclass
class JobConfig:
    """Validated configuration for a processing job."""
    job_id: str
    bucket: str
    input_key: str
    output_prefix: str
    model: str
    metadata: JobMetadata
    track_id: str = ""
    generation: str = ""
    create_multitrack_mp4: bool = True
    aac_bitrate: str = DEFAULT_AAC_BITRATE
    metadata_dict: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        # Normalize output_prefix to always end with /
        self.output_prefix = self.output_prefix.rstrip("/") + "/"


class InputValidationError(ValueError):
    """Raised when job input validation fails."""
    pass


def validate_s3_paths(input_key: str, output_prefix: str) -> tuple[str, str]:
    """
    Validate S3 paths to prevent path traversal and enforce the staging layout.

    Args:
        input_key: e.g. tracks/<track_id>/<generation>/staging/source.mp4
        output_prefix: e.g. tracks/<track_id>/<generation>/staging/

    Returns:
        (track_id, generation) parsed from the output prefix.

    Raises:
        InputValidationError: If paths are invalid or potentially malicious.
    """
    # Check for directory traversal
    if ".." in input_key or ".." in output_prefix:
        raise InputValidationError("Path traversal detected in S3 paths")

    if not ALLOWED_INPUT_PATTERN.match(input_key):
        raise InputValidationError(
            f"Invalid input_key format: {input_key}. "
            f"Expected: tracks/<track_id>/<generation>/staging/source.mp4"
        )

    normalized_prefix = output_prefix.rstrip("/") + "/"
    m = ALLOWED_OUTPUT_PATTERN.match(normalized_prefix)
    if not m:
        raise InputValidationError(
            f"Invalid output_prefix format: {output_prefix}. "
            f"Expected: tracks/<track_id>/<generation>/staging/"
        )
    return m.group("track_id"), m.group("generation")


def validate_input(job_input: dict[str, Any]) -> JobConfig:
    """
    Validate and parse job input into typed configuration.

    Expected shape:
    {
      "job_id": "...",
      "bucket": "...",                                   # optional (env default)
      "input_key": "tracks/<id>/<gen>/staging/source.mp4",
      "output_prefix": "tracks/<id>/<gen>/staging/",
      "model": "htdemucs_6s",                            # optional
      "create_multitrack_mp4": true,                     # optional (env default)
      "aac_bitrate": "256k",                             # optional (env default)
      "metadata": {"title": ..., "artist": ..., "sourceUrl": ..., "videoId": ...}
    }

    Raises:
        InputValidationError: If required fields missing or validation fails.
    """
    job_id = job_input.get("job_id")
    if not job_id:
        raise InputValidationError("Missing required field: job_id")

    input_key = job_input.get("input_key")
    if not input_key:
        raise InputValidationError("Missing required field: input_key")

    output_prefix = job_input.get("output_prefix")
    if not output_prefix:
        raise InputValidationError("Missing required field: output_prefix")

    track_id, generation = validate_s3_paths(input_key, output_prefix)

    bucket = job_input.get("bucket") or os.getenv("AWS_S3_BUCKET")
    if not bucket:
        raise InputValidationError("Missing bucket (input field or AWS_S3_BUCKET env)")

    model = job_input.get("model") or "htdemucs_6s"

    create_multitrack = job_input.get("create_multitrack_mp4")
    if create_multitrack is None:
        create_multitrack = create_multitrack_default()

    bitrate = job_input.get("aac_bitrate") or stem_aac_bitrate()
    if not BITRATE_PATTERN.match(bitrate):
        raise InputValidationError(f"Invalid aac_bitrate: {bitrate!r} (expected e.g. '320k')")

    metadata_dict = job_input.get("metadata", {})
    metadata = JobMetadata.from_dict(metadata_dict)

    return JobConfig(
        job_id=job_id,
        bucket=bucket,
        input_key=input_key,
        output_prefix=output_prefix,
        model=model,
        metadata=metadata,
        track_id=track_id,
        generation=generation,
        create_multitrack_mp4=bool(create_multitrack),
        aac_bitrate=bitrate,
        metadata_dict=metadata_dict,
    )

"""
Configuration and input validation for Runpod Demucs handler.

Validates job inputs and provides typed configuration.
"""

import os
import re
from dataclasses import dataclass
from typing import Any

# S3 path validation patterns
# Allows alphanumeric, underscores, hyphens in job IDs (Supabase record IDs)
ALLOWED_INPUT_PATTERN = re.compile(r'^karaoke/in/[a-zA-Z0-9_-]+/source\.mp4$')
ALLOWED_OUTPUT_PATTERN = re.compile(r'^karaoke/out/[a-zA-Z0-9_-]+/$')


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
    create_canonical_mp4: bool = True

    def __post_init__(self):
        # Normalize output_prefix to always end with /
        self.output_prefix = self.output_prefix.rstrip("/") + "/"


class InputValidationError(ValueError):
    """Raised when job input validation fails."""
    pass


def _env(name: str) -> str:
    """Get required environment variable."""
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def validate_s3_paths(input_key: str, output_prefix: str) -> None:
    """
    Validate S3 paths to prevent path traversal and ensure expected format.

    Args:
        input_key: S3 key for input file (e.g., karaoke/in/recXXX/source.mp4)
        output_prefix: S3 prefix for outputs (e.g., karaoke/out/recXXX/)

    Raises:
        InputValidationError: If paths are invalid or potentially malicious
    """
    # Check for directory traversal
    if ".." in input_key or ".." in output_prefix:
        raise InputValidationError("Path traversal detected in S3 paths")

    # Validate input_key format
    if not ALLOWED_INPUT_PATTERN.match(input_key):
        raise InputValidationError(
            f"Invalid input_key format: {input_key}. "
            f"Expected: karaoke/in/<job_id>/source.mp4"
        )

    # Validate output_prefix format
    normalized_prefix = output_prefix.rstrip("/") + "/"
    if not ALLOWED_OUTPUT_PATTERN.match(normalized_prefix):
        raise InputValidationError(
            f"Invalid output_prefix format: {output_prefix}. "
            f"Expected: karaoke/out/<job_id>/"
        )


def validate_input(job_input: dict[str, Any]) -> JobConfig:
    """
    Validate and parse job input into typed configuration.

    Args:
        job_input: Raw job input dictionary

    Returns:
        JobConfig with validated and typed fields

    Raises:
        InputValidationError: If required fields missing or validation fails
    """
    # Required fields
    job_id = job_input.get("job_id")
    if not job_id:
        raise InputValidationError("Missing required field: job_id")

    input_key = job_input.get("input_key")
    if not input_key:
        raise InputValidationError("Missing required field: input_key")

    output_prefix = job_input.get("output_prefix")
    if not output_prefix:
        raise InputValidationError("Missing required field: output_prefix")

    # Validate S3 paths
    validate_s3_paths(input_key, output_prefix)

    # Optional fields with defaults
    bucket = job_input.get("bucket") or os.getenv("AWS_S3_BUCKET", "karaoke-pimpshizzle")
    model = job_input.get("model") or "htdemucs_6s"
    create_canonical_mp4 = job_input.get("create_canonical_mp4", True)

    # Parse metadata
    metadata_dict = job_input.get("metadata", {})
    metadata = JobMetadata.from_dict(metadata_dict)

    return JobConfig(
        job_id=job_id,
        bucket=bucket,
        input_key=input_key,
        output_prefix=output_prefix,
        model=model,
        metadata=metadata,
        create_canonical_mp4=create_canonical_mp4,
    )

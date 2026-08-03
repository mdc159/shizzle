"""Structured error codes and the stage-failure exception (design spec section 4)."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable machine-readable failure codes stored on jobs.error_code."""

    YTDLP_BLOCKED = "YTDLP_BLOCKED"
    DEMUCS_FAILED = "DEMUCS_FAILED"
    S3_UPLOAD_FAILED = "S3_UPLOAD_FAILED"
    RUNPOD_TIMEOUT = "RUNPOD_TIMEOUT"
    RUNPOD_DISPATCH_FAILED = "RUNPOD_DISPATCH_FAILED"
    CHECKSUM_MISMATCH = "CHECKSUM_MISMATCH"
    INTEGRITY_GATE_FAILED = "INTEGRITY_GATE_FAILED"
    SOURCE_MISSING = "SOURCE_MISSING"
    FFMPEG_FAILED = "FFMPEG_FAILED"
    PUBLISH_FAILED = "PUBLISH_FAILED"
    MAX_ATTEMPTS_EXCEEDED = "MAX_ATTEMPTS_EXCEEDED"
    INTERNAL = "INTERNAL"


class StageError(Exception):
    """A stage handler failed with a structured code.

    retryable=False fails the job immediately (no backoff schedule) — e.g. a
    URL-sourced job hitting the Phase 3 yt-dlp stub, or an integrity gate that
    deterministic re-runs cannot fix.
    """

    def __init__(self, code: ErrorCode, detail: str, *, retryable: bool = True) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.retryable = retryable

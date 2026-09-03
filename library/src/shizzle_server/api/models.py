"""Pydantic response/request schemas for the API (DB-backed since Phase 2)."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..db.models import Job, JobStage, Track

# Backwards-compat alias: Phase 1 tests/clients imported JobStatusEnum.
JobStatusEnum = JobStage


class JobResponse(BaseModel):
    jobId: str
    status: JobStage
    sourceType: str
    title: str | None = None
    artist: str = ""
    attempt: int
    error: str | None = None
    errorCode: str | None = None
    message: str | None = None
    stageTimings: dict[str, Any] | None = None
    workerPhase: str | None = None
    workerHeartbeatAt: datetime | None = None
    trackId: str | None = None
    createdAt: datetime | None = None
    updatedAt: datetime | None = None

    @classmethod
    def from_job(cls, job: Job) -> JobResponse:
        return cls(
            jobId=job.id.hex,
            status=job.status,
            sourceType=job.source_type.value,
            title=job.title,
            artist=job.artist,
            attempt=job.attempt,
            error=job.error_detail,
            errorCode=job.error_code,
            stageTimings=job.stage_timings,
            workerPhase=job.worker_phase,
            workerHeartbeatAt=job.worker_heartbeat_at,
            trackId=str(job.track_id) if job.track_id else None,
            createdAt=job.created_at,
            updatedAt=job.updated_at,
        )


class JobListResponse(BaseModel):
    jobs: list[JobResponse]
    total: int
    limit: int
    offset: int


class SubmitUrlRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    title: str | None = Field(None, max_length=1024)
    artist: str | None = Field(None, max_length=1024)


class AuthRequest(BaseModel):
    passcode: str = Field(min_length=1, max_length=256)


class AuthResponse(BaseModel):
    token: str
    expiresIn: int
    mediaCookies: bool = False


class MediaSessionResponse(BaseModel):
    expiresIn: int
    cloudfront: bool


PlaybackEventName = Literal[
    "session-started",
    "playing",
    "heartbeat",
    "seek-started",
    "seek-settled",
    "pause",
    "ended",
    "replay",
    "visibility-hidden",
    "visibility-visible",
    "play-rejected",
    "stem-media-error",
    "stem-clock-stalled",
    "video-media-error",
    "video-buffering",
    "video-clock-stalled",
    "audio-context-not-running",
    "render-silence",
    "recovery-started",
    "recovery-succeeded",
    "recovery-failed",
    "fatal",
    "session-ended",
]


class PlaybackTelemetryRequest(BaseModel):
    sessionId: uuid.UUID
    trackId: uuid.UUID
    generation: int = Field(ge=1)
    sequence: int = Field(ge=1)
    event: PlaybackEventName
    clientAtMs: int = Field(ge=0)
    appBuild: str = Field(default="unknown", min_length=1, max_length=128)
    browser: str = Field(default="unknown", min_length=1, max_length=256)
    detail: dict[str, Any] | None = None

    @field_validator("detail")
    @classmethod
    def telemetry_detail_is_bounded_and_contains_no_credentials(
        cls, value: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        if value is None:
            return None
        sensitive = {"authorization", "cookie", "password", "passcode", "token", "url"}

        def walk(item: Any) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    lowered = str(key).lower()
                    if any(fragment in lowered for fragment in sensitive):
                        raise ValueError(f"telemetry detail contains forbidden key {key!r}")
                    walk(child)
            elif isinstance(item, list):
                for child in item:
                    walk(child)
            elif isinstance(item, str) and len(item) > 2048:
                raise ValueError("telemetry detail string exceeds 2048 characters")

        walk(value)
        if len(json.dumps(value, separators=(",", ":"), ensure_ascii=False)) > 32_768:
            raise ValueError("telemetry detail exceeds 32768 bytes")
        return value


class PlaybackTelemetryResponse(BaseModel):
    accepted: bool


class PlaybackIncidentInfo(BaseModel):
    sessionId: uuid.UUID
    trackId: uuid.UUID
    trackTitle: str
    generation: int
    sequence: int
    event: str
    clientAtMs: int
    receivedAt: datetime
    detail: dict[str, Any] | None = None


class PlaybackIncidentListResponse(BaseModel):
    since: datetime
    total: int
    incidents: list[PlaybackIncidentInfo]


class TrackInfo(BaseModel):
    id: str
    title: str
    artist: str
    slug: str
    duration: float
    publicUrl: str
    status: str = "ready"

    @classmethod
    def from_track(cls, track: Track) -> TrackInfo:
        return cls(
            id=str(track.id),
            title=track.title,
            artist=track.artist,
            slug=str(track.id),
            duration=track.duration_seconds,
            publicUrl=f"/api/tracks/{track.id}",
        )


class LibraryResponse(BaseModel):
    tracks: list[TrackInfo]
    total: int


class HealthResponse(BaseModel):
    status: str
    api: bool
    db: bool
    orchestratorAlive: bool
    orchestratorLastHeartbeat: datetime | None = None

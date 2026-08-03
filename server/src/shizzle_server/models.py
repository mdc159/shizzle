"""Pydantic response/request schemas for the API (DB-backed since Phase 2)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .db.models import Job, JobStage, Track

# Backwards-compat alias: Phase 1 tests/clients imported JobStatusEnum.
JobStatusEnum = JobStage


class JobResponse(BaseModel):
    jobId: str
    status: JobStage
    sourceType: str
    attempt: int
    error: str | None = None
    errorCode: str | None = None
    message: str | None = None
    stageTimings: dict[str, Any] | None = None
    trackId: str | None = None
    createdAt: datetime | None = None
    updatedAt: datetime | None = None

    @classmethod
    def from_job(cls, job: Job) -> JobResponse:
        return cls(
            jobId=job.id.hex,
            status=job.status,
            sourceType=job.source_type.value,
            attempt=job.attempt,
            error=job.error_detail,
            errorCode=job.error_code,
            stageTimings=job.stage_timings,
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
    title: str | None = None


class AuthRequest(BaseModel):
    passcode: str = Field(min_length=1, max_length=256)


class AuthResponse(BaseModel):
    token: str
    expiresIn: int
    mediaCookies: bool = False


class MediaSessionResponse(BaseModel):
    expiresIn: int
    cloudfront: bool


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

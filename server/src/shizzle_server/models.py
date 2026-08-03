"""Pydantic models for the local karaoke server."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class JobStatusEnum(StrEnum):
    pending = "pending"
    extracting = "extracting"
    splitting = "splitting"
    encoding = "encoding"
    ready = "ready"
    failed = "failed"


class StageTiming(BaseModel):
    stage: str
    start: float
    end: float | None = None
    duration_s: float | None = None


class JobStatus(BaseModel):
    jobId: str
    status: JobStatusEnum
    progress: float | None = None
    message: str | None = None
    error: str | None = None
    timings: list[StageTiming] | None = None


class TrackInfo(BaseModel):
    id: str
    title: str
    artist: str
    slug: str
    duration: float
    publicUrl: str
    status: str = "ready"


class LibraryResponse(BaseModel):
    tracks: list[TrackInfo]
    total: int

"""API routes — DB-backed since Phase 2 (jobs/tracks tables, no in-memory state).

Upload safety is carried verbatim from the k25 lineage: chunked streaming
write with a hard byte cap, ffprobe duration gate, and a path-traversal guard
on file serving. Local file serving remains for the `local` profile; the CDN
path replaces it in Phase 4.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import text

from .db.models import SourceType, Track
from .db.repository import HeartbeatRepository, JobRepository, TrackRepository
from .models import (
    HealthResponse,
    JobListResponse,
    JobResponse,
    LibraryResponse,
    SubmitUrlRequest,
    TrackInfo,
)
from .processing import get_duration
from .settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")


# --- app.state accessors (wired by main.create_app) --------------------------


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _jobs(request: Request) -> JobRepository:
    return request.app.state.job_repo


def _tracks(request: Request) -> TrackRepository:
    return request.app.state.track_repo


def _heartbeats(request: Request) -> HeartbeatRepository:
    return request.app.state.heartbeat_repo


def _parse_job_id(job_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(404, "Job not found") from None


def _parse_track_id(track_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(track_id)
    except ValueError:
        raise HTTPException(404, "Track not found") from None


# --- ingest ------------------------------------------------------------------


@router.post("/upload")
async def upload_file(request: Request, file: UploadFile) -> dict:
    """Accept an MP4 upload, persist it, and create a pending job row."""
    settings = _settings(request)
    if not file.filename:
        raise HTTPException(400, "No file provided")

    if file.content_type and not file.content_type.startswith("video/"):
        raise HTTPException(400, "Only video files are accepted")

    job_id = uuid.uuid4()
    job_dir = settings.data_dir / job_id.hex
    job_dir.mkdir(parents=True, exist_ok=True)

    # Save uploaded file — stream to disk with size limit (carried verbatim),
    # hashing as we go for jobs.input_checksum provenance.
    source_path = job_dir / "source.mp4"

    def _save_upload(src, dst: Path, max_bytes: int) -> tuple[int, str]:
        bytes_written = 0
        digest = hashlib.sha256()
        with open(dst, "wb") as f:
            while chunk := src.read(1024 * 1024):  # 1 MB chunks
                bytes_written += len(chunk)
                if bytes_written > max_bytes:
                    raise ValueError("File too large")
                digest.update(chunk)
                f.write(chunk)
        return bytes_written, digest.hexdigest()

    try:
        _, checksum = await asyncio.to_thread(
            _save_upload, file.file, source_path, settings.max_upload_bytes
        )
    except ValueError:
        shutil.rmtree(job_dir, ignore_errors=True)
        limit_gb = settings.max_upload_bytes // (1024**3)
        raise HTTPException(413, f"File exceeds {limit_gb} GB limit") from None

    # Check duration (ffprobe gate, carried verbatim)
    try:
        duration = await asyncio.to_thread(get_duration, source_path)
        if duration > settings.max_duration_seconds:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(
                413,
                f"Media duration {duration:.0f}s exceeds "
                f"{settings.max_duration_seconds // 60} min limit",
            )
    except HTTPException:
        raise
    except Exception:
        pass  # If ffprobe fails here, let the pipeline handle it

    job = await _jobs(request).create_job(
        job_id=job_id,
        source_type=SourceType.upload,
        source_ref="source.mp4",
        title=Path(file.filename).stem,
        input_checksum=checksum,
        profile_version=settings.processing_profile_version,
    )
    return {"jobId": job.id.hex}


@router.post("/submit-url")
async def submit_url(request: Request, body: SubmitUrlRequest) -> dict:
    """Create a url-sourced job. Phase 2: it will fail at the downloading stub
    with a structured YTDLP_BLOCKED error (Phase 3 implements yt-dlp ingest)."""
    parsed = urlparse(body.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "A valid http(s) URL is required")

    job = await _jobs(request).create_job(
        source_type=SourceType.url,
        source_ref=body.url,
        title=body.title,
        profile_version=_settings(request).processing_profile_version,
    )
    return {"jobId": job.id.hex}


# --- jobs --------------------------------------------------------------------


@router.get("/jobs/{job_id}")
async def get_job_status(request: Request, job_id: str) -> JobResponse:
    job = await _jobs(request).get_job(_parse_job_id(job_id))
    if job is None:
        raise HTTPException(404, "Job not found")
    return JobResponse.from_job(job)


@router.get("/jobs")
async def list_jobs(request: Request, limit: int = 50, offset: int = 0) -> JobListResponse:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    jobs, total = await _jobs(request).list_jobs(limit=limit, offset=offset)
    return JobListResponse(
        jobs=[JobResponse.from_job(j) for j in jobs],
        total=total,
        limit=limit,
        offset=offset,
    )


# --- library / tracks --------------------------------------------------------


@router.get("/library")
async def get_library(request: Request) -> LibraryResponse:
    tracks = await _tracks(request).list_tracks(include_deleted=False)
    infos = [TrackInfo.from_track(t) for t in tracks]
    return LibraryResponse(tracks=infos, total=len(infos))


@router.delete("/tracks/{track_id}")
async def delete_track(request: Request, track_id: str) -> dict:
    """Soft delete (design: manual delete only, no silent retention deletion)."""
    deleted = await _tracks(request).soft_delete(_parse_track_id(track_id))
    if not deleted:
        raise HTTPException(404, "Track not found")
    return {"deleted": track_id}


async def _resolve_local_track_dir(request: Request, track_id: str) -> tuple[Track, Path]:
    track = await _tracks(request).get(_parse_track_id(track_id))
    if track is None or track.deleted_at is not None:
        raise HTTPException(404, "Track not found")
    prefix = track.s3_prefix
    if not prefix.startswith("local/"):
        # Cloud-published tracks are served via CloudFront (Phase 4), not here.
        raise HTTPException(409, "Track is not stored locally")
    job_dir = (_settings(request).data_dir / prefix.removeprefix("local/")).resolve()
    return track, job_dir


@router.get("/tracks/{track_id}/download")
async def download_track(request: Request, track_id: str) -> FileResponse:
    """Download the multi-track MP4 (Mike's archival format)."""
    _, job_dir = await _resolve_local_track_dir(request, track_id)
    mp4 = job_dir / "multi-track.mp4"
    if not mp4.exists():
        raise HTTPException(404, "Multi-track file not found")
    return FileResponse(mp4, media_type="video/mp4", filename=f"{track_id}-multi-track.mp4")


@router.get("/tracks/{track_id}/{path:path}")
async def serve_track_file(request: Request, track_id: str, path: str) -> FileResponse:
    """Serve video, stems, or manifest files for a locally stored track."""
    _, job_dir = await _resolve_local_track_dir(request, track_id)
    file_path = (job_dir / path).resolve()

    # Security: ensure path stays within the job directory (carried verbatim)
    if not file_path.is_relative_to(job_dir):
        raise HTTPException(403, "Access denied")

    if not file_path.exists():
        raise HTTPException(404, f"File not found: {path}")

    media_types = {
        ".mp4": "video/mp4",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".json": "application/json",
    }
    media_type = media_types.get(file_path.suffix.lower(), "application/octet-stream")
    return FileResponse(file_path, media_type=media_type)


# --- health ------------------------------------------------------------------


@router.get("/health")
async def health(request: Request) -> HealthResponse:
    settings = _settings(request)
    db_ok = True
    try:
        engine = request.app.state.engine
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    last_beat = None
    alive = False
    if db_ok:
        try:
            last_beat = await _heartbeats(request).latest()
            if last_beat is not None:
                age = (datetime.now(UTC) - last_beat).total_seconds()
                alive = age <= settings.orchestrator_liveness_seconds
        except Exception:
            pass

    return HealthResponse(
        status="ok" if (db_ok and alive) else "degraded",
        api=True,
        db=db_ok,
        orchestratorAlive=alive,
        orchestratorLastHeartbeat=last_beat,
    )

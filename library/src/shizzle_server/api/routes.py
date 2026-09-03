"""API routes — DB-backed since Phase 2 (jobs/tracks tables, no in-memory state).

Upload safety is carried verbatim from the k25 lineage: chunked streaming
write with a hard byte cap, ffprobe duration gate, and a path-traversal guard
on file serving. Local file serving remains for the `local` profile; the CDN
path replaces it in Phase 4.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import time
import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import text

from ..db.models import SourceType, Track
from ..db.repository import (
    HeartbeatRepository,
    JobRepository,
    PlaybackTelemetryRepository,
    TrackRepository,
)
from ..metadata import resolve_track_metadata
from ..orchestrator.processing import get_duration
from ..settings import Settings
from . import cloudfront, media
from .auth import (
    TOKEN_COOKIE,
    check_passcode,
    create_device_token,
    require_auth,
)
from .models import (
    AuthRequest,
    AuthResponse,
    HealthResponse,
    JobListResponse,
    JobResponse,
    LibraryResponse,
    MediaSessionResponse,
    PlaybackIncidentInfo,
    PlaybackIncidentListResponse,
    PlaybackTelemetryRequest,
    PlaybackTelemetryResponse,
    SubmitUrlRequest,
    TrackInfo,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# Gate every protected route with this. It is a no-op when no passcode is
# configured (local/test), and a 401 on a bad token when one is.
Protected = Depends(require_auth)


def _set_media_cookies(response: Response, settings: Settings) -> bool:
    """Attach the three CloudFront signed cookies scoped to the /cdn path.

    Returns whether cookies were issued (False when CloudFront is unconfigured,
    e.g. the front end is up but CDN media delivery is not wired yet)."""
    if not settings.cloudfront_enabled:
        return False
    cookies = cloudfront.signed_cookies(settings)
    for name, value in cookies.items():
        response.set_cookie(
            name,
            value,
            max_age=settings.media_ttl_seconds,
            path=settings.media_cookie_path,
            secure=True,
            httponly=True,
            samesite="lax",
        )
    return True


# --- app.state accessors (wired by main.create_app) --------------------------


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _jobs(request: Request) -> JobRepository:
    return request.app.state.job_repo


def _tracks(request: Request) -> TrackRepository:
    return request.app.state.track_repo


def _heartbeats(request: Request) -> HeartbeatRepository:
    return request.app.state.heartbeat_repo


def _playback_telemetry(request: Request) -> PlaybackTelemetryRepository:
    return request.app.state.playback_telemetry_repo


class _TelemetryRateLimiter:
    """Small in-process abuse guard; durable events still live in Postgres.

    The playback watchdog can legitimately emit roughly 200 ordered events per
    minute during aggressive seeking (transport, buffering, recovery started,
    recovery settled). The ceiling leaves 3x measured headroom while still
    bounding an authenticated client's database write rate.
    """

    def __init__(self, limit: int = 600, window_seconds: float = 60.0) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[uuid.UUID, deque[float]] = defaultdict(deque)

    def allow(self, session_id: uuid.UUID) -> bool:
        now = time.monotonic()
        cutoff = now - self.window_seconds
        hits = self._hits[session_id]
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


_telemetry_rate_limiter = _TelemetryRateLimiter()


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


# --- auth (design spec §7) ---------------------------------------------------


@router.post("/auth")
async def authenticate(request: Request, body: AuthRequest) -> JSONResponse:
    """Exchange the shared passcode for a device token + media cookies.

    When no passcode is configured the gate is off; we still mint a token so
    the client flow is uniform.
    """
    settings = _settings(request)
    if settings.auth_enabled and not check_passcode(settings, body.passcode):
        raise HTTPException(401, "Incorrect passcode")

    token, expiry = create_device_token(settings)
    # Placeholder body; media-cookie issuance decides the mediaCookies flag,
    # then we re-serialize (set_cookie appends distinct Set-Cookie headers).
    response = JSONResponse({})
    response.set_cookie(
        TOKEN_COOKIE,
        token,
        max_age=settings.auth_token_ttl_seconds,
        path="/",
        secure=True,
        httponly=True,
        samesite="lax",
    )
    issued = _set_media_cookies(response, settings)
    payload = AuthResponse(token=token, expiresIn=expiry, mediaCookies=issued)
    response.body = response.render(payload.model_dump())
    response.headers["content-length"] = str(len(response.body))
    return response


@router.post("/media/session")
async def media_session(request: Request, _auth: None = Protected) -> JSONResponse:
    """Refresh the CloudFront signed cookies for this device (media TTL)."""
    settings = _settings(request)
    payload = MediaSessionResponse(
        expiresIn=settings.media_ttl_seconds, cloudfront=settings.cloudfront_enabled
    )
    response = JSONResponse(payload.model_dump())
    _set_media_cookies(response, settings)
    return response


# --- ingest ------------------------------------------------------------------


@router.post("/upload")
async def upload_file(
    request: Request,
    file: UploadFile,
    title: str | None = Form(None),
    artist: str | None = Form(None),
    _auth: None = Protected,
) -> dict:
    """Accept an MP4 upload, persist it, and create a pending job row.

    Optional multipart fields ``title``/``artist``: when the user typed both
    they win verbatim; a title alone is parsed for its artist; otherwise the
    filename stem is parsed. The resolved pair is stored on the job and
    echoed in the response.
    """
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

    metadata = resolve_track_metadata(title, artist, Path(file.filename).stem)
    job = await _jobs(request).create_job(
        job_id=job_id,
        source_type=SourceType.upload,
        source_ref="source.mp4",
        title=metadata.title,
        artist=metadata.artist,
        input_checksum=checksum,
        profile_version=settings.processing_profile_version,
    )
    return {"jobId": job.id.hex, "title": job.title, "artist": job.artist}


@router.post("/submit-url")
async def submit_url(request: Request, body: SubmitUrlRequest, _auth: None = Protected) -> dict:
    """Create a url-sourced job. Phase 2: it will fail at the downloading stub
    with a structured YTDLP_BLOCKED error (Phase 3 implements yt-dlp ingest)."""
    parsed = urlparse(body.url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "A valid http(s) URL is required")

    metadata = resolve_track_metadata(body.title, body.artist, body.title)
    job = await _jobs(request).create_job(
        source_type=SourceType.url,
        source_ref=body.url,
        title=metadata.title or None,
        artist=metadata.artist,
        profile_version=_settings(request).processing_profile_version,
    )
    return {"jobId": job.id.hex, "title": job.title, "artist": job.artist}


# --- jobs --------------------------------------------------------------------


@router.get("/jobs/{job_id}/events")
async def get_job_events(
    request: Request, job_id: str, _auth: None = Protected
) -> dict[str, list[dict[str, Any]]]:
    parsed_id = _parse_job_id(job_id)
    if await _jobs(request).get_job(parsed_id) is None:
        raise HTTPException(404, "Job not found")
    events = await _jobs(request).list_events(parsed_id)
    return {
        "events": [
            {"event": event.event, "detail": event.detail, "createdAt": event.created_at}
            for event in events
        ]
    }


@router.get("/jobs/{job_id}")
async def get_job_status(request: Request, job_id: str, _auth: None = Protected) -> JobResponse:
    job = await _jobs(request).get_job(_parse_job_id(job_id))
    if job is None:
        raise HTTPException(404, "Job not found")
    return JobResponse.from_job(job)


@router.get("/jobs")
async def list_jobs(
    request: Request, limit: int = 50, offset: int = 0, _auth: None = Protected
) -> JobListResponse:
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
async def get_library(request: Request, _auth: None = Protected) -> LibraryResponse:
    tracks = await _tracks(request).list_tracks(include_deleted=False)
    infos = [TrackInfo.from_track(t) for t in tracks]
    return LibraryResponse(tracks=infos, total=len(infos))


@router.post("/playback/telemetry")
async def ingest_playback_telemetry(
    request: Request,
    body: PlaybackTelemetryRequest,
    _auth: None = Protected,
) -> PlaybackTelemetryResponse:
    """Persist direct browser health evidence without media URLs or credentials."""
    if not _telemetry_rate_limiter.allow(body.sessionId):
        raise HTTPException(
            429,
            "Playback telemetry rate limit exceeded",
            headers={"Retry-After": "5"},
        )
    track = await _tracks(request).get(body.trackId)
    if track is None or track.deleted_at is not None:
        raise HTTPException(404, "Track not found")
    try:
        accepted = await _playback_telemetry(request).ingest(
            session_id=body.sessionId,
            track_id=body.trackId,
            generation=body.generation,
            sequence=body.sequence,
            event=body.event,
            client_at_ms=body.clientAtMs,
            app_build=body.appBuild,
            browser=body.browser,
            detail=body.detail,
        )
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return PlaybackTelemetryResponse(accepted=accepted)


@router.get("/playback/incidents")
async def recent_playback_incidents(
    request: Request,
    minutes: int = Query(default=15, ge=1, le=1440),
    limit: int = Query(default=100, ge=1, le=1000),
    _auth: None = Protected,
) -> PlaybackIncidentListResponse:
    """Query first-party direct playback failures by track and generation."""
    since = datetime.now(UTC) - timedelta(minutes=minutes)
    rows = await _playback_telemetry(request).recent_incidents(since=since, limit=limit)
    incidents = [
        PlaybackIncidentInfo(
            sessionId=event.session_id,
            trackId=event.track_id,
            trackTitle=title,
            generation=event.generation,
            sequence=event.sequence,
            event=event.event,
            clientAtMs=event.client_at_ms,
            receivedAt=event.received_at,
            detail=event.detail,
        )
        for event, title in rows
    ]
    return PlaybackIncidentListResponse(since=since, total=len(incidents), incidents=incidents)


@router.delete("/tracks/{track_id}")
async def delete_track(request: Request, track_id: str, _auth: None = Protected) -> dict:
    """Soft delete (design: manual delete only, no silent retention deletion)."""
    deleted = await _tracks(request).soft_delete(_parse_track_id(track_id))
    if not deleted:
        raise HTTPException(404, "Track not found")
    return {"deleted": track_id}


@router.get("/tracks/{track_id}/manifest")
async def get_track_manifest(
    request: Request, track_id: str, _auth: None = Protected
) -> dict[str, Any]:
    """Return the playback manifest with media refs the player can fetch.

    Cloud tracks (S3 prefix ``tracks/…``): read manifest.json from S3 and
    rewrite ``video`` + ``stems[].file`` to same-origin ``/cdn`` paths that
    Caddy proxies to CloudFront (signed-cookie gated). Local tracks: read the
    on-disk manifest and leave refs relative (the player joins them with the
    ``/api/tracks/{id}`` base). Declared before the ``{path:path}`` catch-all so
    it wins the route match.
    """
    settings = _settings(request)
    track = await _tracks(request).get(_parse_track_id(track_id))
    if track is None or track.deleted_at is not None:
        raise HTTPException(404, "Track not found")

    if track.s3_prefix.startswith("local/"):
        _, job_dir = await _resolve_local_track_dir(request, track_id)
        manifest_path = (job_dir / "stems.json").resolve()
        if not manifest_path.is_relative_to(job_dir) or not manifest_path.exists():
            raise HTTPException(404, "Manifest not found")
        return dict(json.loads(manifest_path.read_text()))

    # Cloud track: pull from S3 and rewrite to /cdn paths.
    try:
        raw = await asyncio.to_thread(media.load_s3_manifest, settings, track.manifest_key)
    except Exception as exc:  # noqa: BLE001 - surface as 502 with a clean message
        logger.warning("manifest fetch failed for %s: %s", track_id, exc)
        raise HTTPException(502, "Manifest unavailable") from exc
    rewritten = media.rewrite_cloud_manifest(settings, raw, track.s3_prefix)
    rewritten["track_id"] = str(track.id)
    rewritten["generation"] = track.generation
    return rewritten


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
async def download_track(request: Request, track_id: str, _auth: None = Protected) -> FileResponse:
    """Download the multi-track MP4 (Mike's archival format)."""
    _, job_dir = await _resolve_local_track_dir(request, track_id)
    mp4 = job_dir / "multi-track.mp4"
    if not mp4.exists():
        raise HTTPException(404, "Multi-track file not found")
    return FileResponse(mp4, media_type="video/mp4", filename=f"{track_id}-multi-track.mp4")


@router.get("/tracks/{track_id}/{path:path}")
async def serve_track_file(
    request: Request, track_id: str, path: str, _auth: None = Protected
) -> FileResponse:
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
        authGate=settings.auth_gate_state,
    )

"""API routes for local karaoke server."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, UploadFile
from fastapi.responses import FileResponse

from .models import JobStatus, JobStatusEnum, LibraryResponse, StageTiming, TrackInfo
from .processing import get_duration, run_pipeline

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api")

# In-memory job tracking
jobs: dict[str, JobStatus] = {}

# Simple lock: one pipeline at a time
_pipeline_lock = asyncio.Lock()

# Track background tasks for graceful shutdown
_tasks: set[asyncio.Task] = set()

# Data directory (set by main.py on startup)
DATA_DIR: Path = Path()

# --- Limits ---
MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GB
MAX_DURATION_SECONDS = 1800  # 30 minutes
MAX_JOBS = 20


def set_data_dir(path: Path) -> None:
    global DATA_DIR
    DATA_DIR = path


def scan_library() -> list[TrackInfo]:
    """Scan data/ for completed tracks (those with stems.json)."""
    tracks = []
    if not DATA_DIR.exists():
        return tracks

    for job_dir in sorted(DATA_DIR.iterdir()):
        if not job_dir.is_dir():
            continue
        manifest = job_dir / "stems.json"
        if not manifest.exists():
            continue

        try:
            data = json.loads(manifest.read_text())
        except Exception:
            continue

        tracks.append(TrackInfo(
            id=job_dir.name,
            title=data.get("title", job_dir.name),
            artist=data.get("artist", ""),
            slug=job_dir.name,
            duration=data.get("duration", 0),
            publicUrl=f"/api/tracks/{job_dir.name}",
        ))

    return tracks


def _enforce_retention() -> None:
    """Delete oldest job directories when count exceeds MAX_JOBS."""
    if not DATA_DIR.exists():
        return
    job_dirs = sorted(
        (d for d in DATA_DIR.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
    )
    while len(job_dirs) > MAX_JOBS:
        oldest = job_dirs.pop(0)
        logger.info("Retention: removing old job %s", oldest.name)
        shutil.rmtree(oldest, ignore_errors=True)
        jobs.pop(oldest.name, None)


@router.post("/upload")
async def upload_file(file: UploadFile) -> dict:
    """Accept an MP4 upload and start the processing pipeline."""
    if not file.filename:
        raise HTTPException(400, "No file provided")

    if file.content_type and not file.content_type.startswith("video/"):
        raise HTTPException(400, "Only video files are accepted")

    # Enforce retention before creating new job
    _enforce_retention()

    job_id = uuid.uuid4().hex[:12]
    job_dir = DATA_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Save uploaded file — stream to disk with size limit
    source_path = job_dir / "source.mp4"

    def _save_upload(src, dst: Path) -> int:
        bytes_written = 0
        with open(dst, "wb") as f:
            while chunk := src.read(1024 * 1024):  # 1 MB chunks
                bytes_written += len(chunk)
                if bytes_written > MAX_UPLOAD_BYTES:
                    raise ValueError("File too large")
                f.write(chunk)
        return bytes_written

    try:
        await asyncio.to_thread(_save_upload, file.file, source_path)
    except ValueError:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise HTTPException(413, f"File exceeds {MAX_UPLOAD_BYTES // (1024**3)} GB limit") from None

    # Check duration
    try:
        duration = await asyncio.to_thread(get_duration, source_path)
        if duration > MAX_DURATION_SECONDS:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise HTTPException(
                413,
                f"Media duration {duration:.0f}s exceeds {MAX_DURATION_SECONDS // 60} min limit",
            )
    except HTTPException:
        raise
    except Exception:
        pass  # If ffprobe fails here, let the pipeline handle it

    # Derive title from original filename
    original_title = Path(file.filename).stem

    # Initialize job status
    jobs[job_id] = JobStatus(jobId=job_id, status=JobStatusEnum.pending)

    # Start pipeline in background
    task = asyncio.create_task(_run_pipeline(job_id, job_dir, source_path, original_title))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)

    return {"jobId": job_id}


async def _run_pipeline(job_id: str, job_dir: Path, source_path: Path, title: str | None = None) -> None:
    """Run the pipeline in background, updating job status."""
    async with _pipeline_lock:
        timings: list[StageTiming] = []

        def on_status(status: str, message: str) -> None:
            now = time.monotonic()
            # Close previous stage timing
            if timings and timings[-1].end is None:
                timings[-1].end = now
                timings[-1].duration_s = round(now - timings[-1].start, 2)
            # Open new stage timing
            timings.append(StageTiming(stage=status, start=now))
            jobs[job_id] = JobStatus(
                jobId=job_id,
                status=JobStatusEnum(status),
                message=message,
                timings=timings,
            )

        try:
            await asyncio.to_thread(run_pipeline, job_dir, source_path, on_status, title)
            # Close final stage timing
            now = time.monotonic()
            if timings and timings[-1].end is None:
                timings[-1].end = now
                timings[-1].duration_s = round(now - timings[-1].start, 2)
            jobs[job_id] = JobStatus(
                jobId=job_id,
                status=JobStatusEnum.ready,
                timings=timings,
            )
        except Exception as e:
            logger.exception("Pipeline failed for job %s", job_id)
            now = time.monotonic()
            if timings and timings[-1].end is None:
                timings[-1].end = now
                timings[-1].duration_s = round(now - timings[-1].start, 2)
            jobs[job_id] = JobStatus(
                jobId=job_id,
                status=JobStatusEnum.failed,
                error=str(e),
                timings=timings,
            )


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str) -> JobStatus:
    """Get processing status for a job."""
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    return jobs[job_id]


@router.get("/library")
async def get_library() -> LibraryResponse:
    """List all completed tracks."""
    tracks = scan_library()
    return LibraryResponse(tracks=tracks, total=len(tracks))


@router.get("/tracks/{job_id}/download")
async def download_track(job_id: str) -> FileResponse:
    """Download the multi-track MP4."""
    mp4 = DATA_DIR / job_id / "multi-track.mp4"
    if not mp4.exists():
        raise HTTPException(404, "Multi-track file not found")
    return FileResponse(
        mp4,
        media_type="video/mp4",
        filename=f"{job_id}-multi-track.mp4",
    )


@router.get("/tracks/{job_id}/{path:path}")
async def serve_track_file(job_id: str, path: str) -> FileResponse:
    """Serve video, stems, or manifest files for a track."""
    file_path = (DATA_DIR / job_id / path).resolve()

    # Security: ensure path stays within job directory
    job_dir = (DATA_DIR / job_id).resolve()
    if not file_path.is_relative_to(job_dir):
        raise HTTPException(403, "Access denied")

    if not file_path.exists():
        raise HTTPException(404, f"File not found: {path}")

    # Determine media type
    suffix = file_path.suffix.lower()
    media_types = {
        ".mp4": "video/mp4",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".json": "application/json",
    }
    media_type = media_types.get(suffix, "application/octet-stream")

    return FileResponse(file_path, media_type=media_type)

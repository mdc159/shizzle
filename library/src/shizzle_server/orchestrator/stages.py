"""Stage handlers: one async function per claimable job status.

Contract: a handler receives a StageContext, does idempotent work, and returns
the next JobStage. Failures raise StageError with a structured code. Handlers
must tolerate re-execution after a crash — all on-disk work is marker/manifest
guarded (see pipelines.py) and the publishing transaction is deterministic-id
idempotent (see repository.publish_track).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..db.models import Job, JobStage, SourceType, utcnow
from ..db.repository import JobRepository, track_id_for_job
from ..errors import ErrorCode, StageError
from ..settings import Settings
from . import cloud
from .pipelines import Pipeline
from .runpod_client import RunPodClient, parse_worker_progress


def _age_seconds(since: datetime | None) -> float | None:
    """Seconds elapsed since ``since``; SQLite naive datetimes treated as UTC."""
    if since is None:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return (utcnow() - since).total_seconds()


@dataclass
class StageContext:
    job: Job
    settings: Settings
    pipeline: Pipeline
    jobs: JobRepository
    runpod: RunPodClient
    # Set by handle_publishing when it commits the track+ready transaction
    # itself; the loop then skips its own advance() call.
    published: bool = False
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def job_dir(self) -> Path:
        return self.settings.data_dir / self.job.id.hex

    @property
    def source_path(self) -> Path:
        return self.job_dir / self.job.source_ref


async def handle_pending(ctx: StageContext) -> JobStage:
    """Bookkeeping start-of-life transition."""
    ctx.job_dir.mkdir(parents=True, exist_ok=True)
    return JobStage.downloading


async def handle_downloading(ctx: StageContext) -> JobStage:
    """Get the source video onto local disk.

    Upload-sourced jobs already streamed to disk in POST /api/upload, so this
    is a presence check + no-op transition. URL-sourced jobs are a Phase 3
    stub (ingest/ytdlp.py): they fail here with a clean structured
    YTDLP_BLOCKED error — correct Phase 2 behavior.
    """
    if ctx.job.source_type == SourceType.url:
        # TODO(Phase 3, 3.3): ingest/ytdlp.py — YouTube-domain-validated URLs,
        # browser-cookie auth, structured YTDLP_BLOCKED on real blocks.
        raise StageError(
            ErrorCode.YTDLP_BLOCKED,
            "URL ingest is not implemented yet (Phase 3); upload the file directly",
            retryable=False,
        )
    if not ctx.source_path.exists():
        raise StageError(
            ErrorCode.SOURCE_MISSING,
            f"uploaded source not found on disk: {ctx.source_path.name}",
            retryable=False,
        )
    if ctx.settings.cloud_pipeline:
        # Cloud path: ship the source to S3 and hand off to the RunPod worker.
        # Local Demucs (splitting) never runs — `dispatched` IS the split.
        await cloud.upload_source(ctx)
        return JobStage.dispatched
    return JobStage.splitting


async def handle_dispatched(ctx: StageContext) -> JobStage | None:
    """One claim-based reconciliation pass for the RunPod split (decision 4).

    Returns ``None`` to park (recheck in ``runpod_poll_seconds``); a stage to
    advance to; or raises a retryable ``StageError`` (bounded by the attempt
    cap). A RunPod job we have already marked dead (``worker_phase == "failed"``)
    is treated as absent so a retry dispatches fresh under a new idempotency key.
    """
    job = ctx.job
    track_id = track_id_for_job(job.id)
    payload = {
        "track_id": str(track_id),
        "generation": 1,
        "bucket": ctx.settings.s3_media_bucket,
        "input_key": cloud.source_key(track_id),
        "output_prefix": cloud.separation_prefix(track_id),
    }

    if job.runpod_job_id and job.worker_phase != "failed":
        status_payload = await ctx.runpod.poll(job.runpod_job_id)
        status, phase = parse_worker_progress(status_payload)

        if status == "COMPLETED":
            await ctx.jobs.append_event(
                job.id, "worker_completed", detail=status_payload.get("output") or {}
            )
            return JobStage.verifying

        if status in ("FAILED", "CANCELLED", "TIMED_OUT"):
            await ctx.jobs.record_worker_progress(job.id, phase="failed")
            raise StageError(
                ErrorCode.RUNPOD_DISPATCH_FAILED,
                f"RunPod job {job.runpod_job_id} {status}: {_runpod_error(status_payload)}",
                retryable=True,
            )

        if status == "IN_QUEUE":
            queued_for = _age_seconds(job.updated_at)
            if queued_for is not None and queued_for > ctx.settings.runpod_queue_timeout_seconds:
                await ctx.runpod.cancel(job.runpod_job_id)
                await ctx.jobs.record_worker_progress(job.id, phase="failed")
                raise StageError(
                    ErrorCode.RUNPOD_TIMEOUT,
                    f"RunPod queued {queued_for:.0f}s > "
                    f"{ctx.settings.runpod_queue_timeout_seconds:.0f}s",
                    retryable=True,
                )
            await ctx.jobs.record_worker_progress(job.id, phase="queued")
            return None

        if status == "IN_PROGRESS":
            stalled_for = _age_seconds(job.worker_heartbeat_at)
            if stalled_for is not None and (
                stalled_for > ctx.settings.runpod_worker_stall_seconds
            ):
                await ctx.runpod.cancel(job.runpod_job_id)
                await ctx.jobs.record_worker_progress(job.id, phase="failed")
                raise StageError(
                    ErrorCode.RUNPOD_TIMEOUT,
                    f"RunPod worker stalled (heartbeat age > "
                    f"{ctx.settings.runpod_worker_stall_seconds:.0f}s)",
                    retryable=True,
                )
            if phase:
                await ctx.jobs.record_worker_progress(job.id, phase=phase)
            return None

        return None  # unknown status — park and re-poll next interval

    # No live RunPod job (fresh, or the previous one died): dispatch.
    runpod_id = await ctx.runpod.dispatch(
        job_id=job.id,
        idempotency_key=f"{job.id.hex}:{job.attempt}",
        payload=payload,
    )
    await ctx.jobs.record_dispatch(job.id, runpod_job_id=runpod_id)
    return None


def _runpod_error(payload: dict[str, Any]) -> str:
    """Best-effort human detail from a RunPod failure payload."""
    for key in ("error", "errors"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value[:200]
    output = payload.get("output")
    if isinstance(output, dict):
        for key in ("error", "message"):
            value = output.get(key)
            if isinstance(value, str) and value:
                return value[:200]
    return ""


async def handle_splitting(ctx: StageContext) -> JobStage:
    """Run the media pipeline (local Demucs path in Phase 2)."""
    title = ctx.job.title or ctx.source_path.stem
    sub_timings = await ctx.pipeline.split(ctx.job_dir, ctx.source_path, title)
    if sub_timings:
        ctx.detail["substage_timings"] = sub_timings
    return JobStage.verifying


async def handle_verifying(ctx: StageContext) -> JobStage:
    """Structural artifact check; cloud path re-proves the worker package."""
    if ctx.settings.cloud_pipeline:
        return await cloud.cloud_verifying(ctx)
    ctx.detail["verify"] = await ctx.pipeline.verify(ctx.job_dir)
    return JobStage.publishing


async def handle_publishing(ctx: StageContext) -> JobStage:
    """Write the tracks row and finish.

    Cloud path: transform the verified package into a browser generation and
    publish immutably (manifest last) via JobRepository.publish_track. Local
    path: reuse the on-disk pipeline describe + a local placeholder prefix.
    """
    if ctx.settings.cloud_pipeline:
        return await cloud.cloud_publishing(ctx)
    manifest = await ctx.pipeline.describe(ctx.job_dir)
    local_prefix = f"local/{ctx.job.id.hex}"
    try:
        await ctx.jobs.publish_track(
            ctx.job.id,
            title=manifest.get("title") or ctx.job.title or ctx.job.id.hex,
            artist=manifest.get("artist", ""),
            duration_seconds=float(manifest.get("duration", 0.0)),
            s3_prefix=local_prefix,
            manifest_key=f"{local_prefix}/stems.json",
            generation=1,
            integrity=manifest.get("integrity"),
        )
    except StageError:
        raise
    except Exception as e:
        raise StageError(ErrorCode.PUBLISH_FAILED, str(e)[:500]) from e
    ctx.published = True
    return JobStage.ready


HANDLERS = {
    JobStage.pending: handle_pending,
    JobStage.downloading: handle_downloading,
    JobStage.dispatched: handle_dispatched,
    JobStage.splitting: handle_splitting,
    JobStage.verifying: handle_verifying,
    JobStage.publishing: handle_publishing,
}

"""Stage handlers: one async function per claimable job status.

Contract: a handler receives a StageContext, does idempotent work, and returns
the next JobStage. Failures raise StageError with a structured code. Handlers
must tolerate re-execution after a crash — all on-disk work is marker/manifest
guarded (see pipelines.py) and the publishing transaction is deterministic-id
idempotent (see repository.publish_track).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..db.models import Job, JobStage, SourceType
from ..db.repository import JobRepository
from ..errors import ErrorCode, StageError
from ..settings import Settings
from .pipelines import Pipeline
from .runpod_client import RunPodClient


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
    # Phase 2: local processing path. TODO(Phase 3): when cloud mode is
    # configured, upload source to S3 staging and return JobStage.dispatched
    # via ctx.runpod.dispatch(...) instead.
    return JobStage.splitting


async def handle_dispatched(_ctx: StageContext) -> JobStage:
    """Phase 3 seam — never claimed by the Phase 2 loop (not in RUNNABLE_STAGES).

    TODO(Phase 3, 3.2): webhook receiver + polling reconciler own this stage;
    this handler becomes the reconciler's recovery check.
    """
    raise StageError(
        ErrorCode.RUNPOD_DISPATCH_FAILED,
        "dispatched stage requires the Phase 3 RunPod client",
        retryable=False,
    )


async def handle_splitting(ctx: StageContext) -> JobStage:
    """Run the media pipeline (local Demucs path in Phase 2)."""
    title = ctx.job.title or ctx.source_path.stem
    sub_timings = await ctx.pipeline.split(ctx.job_dir, ctx.source_path, title)
    if sub_timings:
        ctx.detail["substage_timings"] = sub_timings
    return JobStage.verifying


async def handle_verifying(ctx: StageContext) -> JobStage:
    """Structural artifact check (Phase 3 adds staged-checksum verification)."""
    ctx.detail["verify"] = await ctx.pipeline.verify(ctx.job_dir)
    return JobStage.publishing


async def handle_publishing(ctx: StageContext) -> JobStage:
    """Write the tracks row (generation 1, local prefix placeholder) and finish.

    TODO(Phase 3, 3.4): the cloud publisher takes this over — verify staged
    sha256s, copy staging -> immutable generation prefix, manifest last.
    """
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

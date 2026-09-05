"""Stage handlers: one async function per claimable job status.

Contract: a handler receives a StageContext, does idempotent work, and returns
the next JobStage. Failures raise StageError with a structured code. Handlers
must tolerate re-execution after a crash — all on-disk work is marker/manifest
guarded (see pipelines.py) and the publishing transaction is deterministic-id
idempotent (see repository.publish_track).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..db.models import Job, JobStage, SourceType, utcnow
from ..db.repository import (
    InvalidTransition,
    JobRepository,
    PublishRefusedError,
    pending_runpod_dispatch,
    track_id_for_job,
)
from ..errors import ErrorCode, StageError
from ..settings import Settings
from . import cloud
from .pipelines import Pipeline
from .runpod_client import NotConfiguredRunPodClient, RunPodClient, parse_worker_progress

logger = logging.getLogger(__name__)


def _age_seconds(since: datetime | None) -> float | None:
    """Seconds elapsed since ``since``; SQLite naive datetimes treated as UTC."""
    if since is None:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return (utcnow() - since).total_seconds()


async def _cancel_best_effort(
    ctx: StageContext, runpod_job_id: str, *, reason: str
) -> None:
    """Cancel a RunPod job without masking the triggering error.

    A non-retryable ``cancel`` failure must never replace the original error.
    In the stale-worker path that original error is a *transient* poll failure
    meant to be retried; letting cancel replace it would permanently fail the
    job on an unrelated API blip. We log the cancel failure and let the caller
    fail the worker and raise whatever error it intended (wave3 #1).
    """
    try:
        await ctx.runpod.cancel(runpod_job_id)
    except Exception:
        logger.exception(
            "job %s: cancel failed for %s (%s); failing the worker anyway",
            ctx.job.id,
            runpod_job_id,
            reason,
        )


async def _confirm_dispatch(
    ctx: StageContext, *, idempotency_key: str, runpod_job_id: str
) -> None:
    """Retry the small DB confirmation window after RunPod accepts a job."""
    delays = (0.0, 0.1, 0.5)
    for index, delay in enumerate(delays):
        if delay:
            await asyncio.sleep(delay)
        try:
            await ctx.jobs.confirm_dispatch(
                ctx.job.id,
                idempotency_key=idempotency_key,
                runpod_job_id=runpod_job_id,
            )
            return
        except InvalidTransition:
            raise
        except Exception:
            if index == len(delays) - 1:
                raise
            logger.warning(
                "job %s: retrying persistence of RunPod id %s",
                ctx.job.id,
                runpod_job_id,
                exc_info=True,
            )


@dataclass
class StageContext:
    job: Job
    settings: Settings
    pipeline: Pipeline
    jobs: JobRepository
    runpod: RunPodClient
    worker_id: str
    # Set by handle_publishing when it commits the track+ready transaction
    # itself; the loop then skips its own advance() call.
    published: bool = False
    detail: dict[str, Any] = field(default_factory=dict)
    park_seconds: float | None = None

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
        try:
            status_payload = await ctx.runpod.poll(job.runpod_job_id)
        except StageError as exc:
            transient = exc.retryable and exc.code in {
                ErrorCode.RUNPOD_TIMEOUT,
                ErrorCode.RUNPOD_DISPATCH_FAILED,
            }
            stalled_for = _age_seconds(job.worker_heartbeat_at)
            if not transient:
                raise
            if isinstance(ctx.runpod, NotConfiguredRunPodClient):
                # Configuration outage (parked cloud): an unconfigured client
                # can never observe the remote job, and its heartbeat can
                # never refresh while polling is impossible — the stall
                # watchdog below would eventually kill a live, paid RunPod
                # job that nothing is wrong with. Park until credentials
                # return; the job reconciles on the first real poll.
                logger.info(
                    "job %s: RunPod not configured; parking dispatched job %s",
                    job.id,
                    job.runpod_job_id,
                )
                return None
            if (
                stalled_for is not None
                and stalled_for > ctx.settings.runpod_worker_stall_seconds
            ):
                if not await ctx.jobs.owns_lease(job.id, ctx.worker_id):
                    # B2: lease lost — a stale worker must not cancel the new
                    # owner's RunPod job or fail its job; yield (the loop's
                    # park/release no-op on the owner guard).
                    logger.info(
                        "job %s: lease lost; skipping worker-failure handling", job.id
                    )
                    return None
                await _cancel_best_effort(
                    ctx, job.runpod_job_id, reason="stale worker heartbeat"
                )
                await ctx.jobs.record_worker_progress(
                    job.id, phase="failed", worker_id=ctx.worker_id
                )
                raise
            # wave3 #3: dedup runpod_poll_failed to one row per outage, like the
            # bounded history in record_worker_progress. The most recent event
            # being a runpod_poll_failed means we are still inside the same
            # outage; any other event (dispatch, worker_completed, phase change,
            # …) resets it so the next outage gets a fresh row.
            events = await ctx.jobs.list_events(job.id)
            if not events or events[-1].event != "runpod_poll_failed":
                await ctx.jobs.append_event(
                    job.id,
                    "runpod_poll_failed",
                    {"error_code": exc.code.value, "detail": exc.detail[:500]},
                    worker_id=ctx.worker_id,
                )
            logger.warning("job %s: transient RunPod poll failure: %s", job.id, exc)
            return None
        status, phase = parse_worker_progress(status_payload)

        events = await ctx.jobs.list_events(job.id)
        if events and events[-1].event == "runpod_poll_failed":
            await ctx.jobs.append_event(
                job.id, "runpod_poll_recovered", worker_id=ctx.worker_id
            )

        if status == "COMPLETED":
            output = status_payload.get("output")
            detail = output if isinstance(output, dict) else {"output": output}
            events = await ctx.jobs.list_events(job.id)
            dispatch_key = _confirmed_dispatch_key(events, job.runpod_job_id)
            # Older workers return SUPERSEDED after losing a receipt claim,
            # even if its owner died before publishing the package (A1/B12).
            # Verify the marker at the durable dispatch's prefix, never at an
            # output-supplied location, before recording completion.
            if detail.get("status") == "SUPERSEDED" and not await cloud.package_ready(
                ctx, idempotency_key=dispatch_key
            ):
                await ctx.jobs.record_worker_progress(
                    job.id, phase="failed", worker_id=ctx.worker_id
                )
                raise StageError(
                    ErrorCode.RUNPOD_DISPATCH_FAILED,
                    f"RunPod job {job.runpod_job_id} SUPERSEDED without handoff.json",
                    retryable=True,
                )
            if not any(event.event == "worker_completed" for event in events):
                await ctx.jobs.append_event(
                    job.id,
                    "worker_completed",
                    detail=detail,
                    worker_id=ctx.worker_id,
                )
            if dispatch_key is not None:
                ctx.detail["package_prefix"] = cloud.attempt_prefix(
                    track_id, dispatch_key
                )
            return JobStage.verifying

        if status in ("FAILED", "CANCELLED", "TIMED_OUT"):
            await ctx.jobs.record_worker_progress(
                job.id, phase="failed", worker_id=ctx.worker_id
            )
            raise StageError(
                ErrorCode.RUNPOD_DISPATCH_FAILED,
                f"RunPod job {job.runpod_job_id} {status}: {_runpod_error(status_payload)}",
                retryable=True,
            )

        if status == "IN_QUEUE":
            queued_for = _age_seconds(job.worker_heartbeat_at)
            if queued_for is not None and queued_for > ctx.settings.runpod_queue_timeout_seconds:
                if not await ctx.jobs.owns_lease(job.id, ctx.worker_id):
                    # B2: lease lost — never cancel the new owner's RunPod job.
                    logger.info(
                        "job %s: lease lost; skipping worker-failure handling", job.id
                    )
                    return None
                await _cancel_best_effort(ctx, job.runpod_job_id, reason="queue timeout")
                await ctx.jobs.record_worker_progress(
                    job.id, phase="failed", worker_id=ctx.worker_id
                )
                raise StageError(
                    ErrorCode.RUNPOD_TIMEOUT,
                    f"RunPod queued {queued_for:.0f}s > "
                    f"{ctx.settings.runpod_queue_timeout_seconds:.0f}s",
                    retryable=True,
                )
            await ctx.jobs.record_worker_progress(
                job.id, phase="queued", worker_id=ctx.worker_id
            )
            return None

        if status == "IN_PROGRESS":
            stalled_for = _age_seconds(job.worker_heartbeat_at)
            if stalled_for is not None and (
                stalled_for > ctx.settings.runpod_worker_stall_seconds
            ):
                if not await ctx.jobs.owns_lease(job.id, ctx.worker_id):
                    # B2: lease lost — never cancel the new owner's RunPod job.
                    logger.info(
                        "job %s: lease lost; skipping worker-failure handling", job.id
                    )
                    return None
                await _cancel_best_effort(ctx, job.runpod_job_id, reason="worker stall")
                await ctx.jobs.record_worker_progress(
                    job.id, phase="failed", worker_id=ctx.worker_id
                )
                raise StageError(
                    ErrorCode.RUNPOD_TIMEOUT,
                    f"RunPod worker stalled (heartbeat age > "
                    f"{ctx.settings.runpod_worker_stall_seconds:.0f}s)",
                    retryable=True,
                )
            if phase:
                await ctx.jobs.record_worker_progress(
                    job.id, phase=phase, worker_id=ctx.worker_id
                )
            return None

        return None  # unknown status — park and re-poll next interval

    # No live RunPod job (fresh, or the previous one died): reconcile or dispatch.
    events = await ctx.jobs.list_events(job.id)
    pending_dispatch = pending_runpod_dispatch(events)
    if pending_dispatch is not None:
        detail = (
            pending_dispatch.detail
            if isinstance(pending_dispatch.detail, dict)
            else {}
        )
        raw_dispatch_key = detail.get("idempotency_key")
        dispatch_key = raw_dispatch_key if isinstance(raw_dispatch_key, str) else None
        accepted_runpod_id = await cloud.accepted_dispatch_id(
            ctx, idempotency_key=dispatch_key
        )
        if accepted_runpod_id is not None:
            if dispatch_key is None:
                await ctx.jobs.record_dispatch(
                    job.id,
                    worker_id=ctx.worker_id,
                    runpod_job_id=accepted_runpod_id,
                )
            else:
                await _confirm_dispatch(
                    ctx,
                    idempotency_key=dispatch_key,
                    runpod_job_id=accepted_runpod_id,
                )
            recovery_detail = {
                "runpod_job_id": accepted_runpod_id,
                "source": cloud.DISPATCH_RECEIPT,
            }
            if dispatch_key is not None:
                recovery_detail["idempotency_key"] = dispatch_key
            await ctx.jobs.append_event(
                job.id,
                "runpod_dispatch_recovered",
                recovery_detail,
                worker_id=ctx.worker_id,
            )
            ctx.park_seconds = ctx.settings.runpod_poll_seconds
            return None
        if await cloud.package_ready(ctx, idempotency_key=dispatch_key):
            already_recovered = any(
                event.event == "runpod_dispatch_recovered"
                and isinstance(event.detail, dict)
                and event.detail.get("idempotency_key") == dispatch_key
                for event in events
            )
            if not already_recovered:
                await ctx.jobs.append_event(
                    job.id,
                    "runpod_dispatch_recovered",
                    {"idempotency_key": dispatch_key, "source": "handoff.json"},
                    worker_id=ctx.worker_id,
                )
            ctx.detail["package_prefix"] = (
                cloud.attempt_prefix(track_id, dispatch_key)
                if dispatch_key is not None
                else cloud.separation_prefix(track_id)
            )
            return JobStage.verifying
        pending_for = _age_seconds(pending_dispatch.created_at) or 0.0
        fail_closed_after = (
            ctx.settings.runpod_queue_timeout_seconds
            + ctx.settings.runpod_worker_stall_seconds
        )
        if pending_for > fail_closed_after:
            failure_detail: dict[str, Any] = {
                "pending_seconds": round(pending_for, 1),
                "manual_action": "resubmit source after confirming no RunPod job is active",
            }
            if dispatch_key is not None:
                failure_detail["idempotency_key"] = dispatch_key
            await ctx.jobs.append_event(
                job.id,
                "runpod_dispatch_reconciliation_failed",
                failure_detail,
                worker_id=ctx.worker_id,
            )
            logger.error(
                "job %s: RunPod dispatch identity unresolved after %.0fs; "
                "failing closed for explicit operator recovery",
                job.id,
                pending_for,
            )
            raise StageError(
                ErrorCode.RUNPOD_TIMEOUT,
                "RunPod may have accepted the dispatch, but no receipt or package "
                "appeared before the reconciliation deadline; manual resubmission "
                "is required after confirming no remote job is active",
                retryable=False,
            )
        ctx.park_seconds = ctx.settings.runpod_poll_seconds
        return None

    dispatch_key = f"{job.id.hex}:{job.attempt}"
    payload["idempotency_key"] = dispatch_key
    reserved = await ctx.jobs.reserve_dispatch(
        job.id,
        worker_id=ctx.worker_id,
        idempotency_key=dispatch_key,
    )
    if not reserved:
        ctx.park_seconds = ctx.settings.runpod_poll_seconds
        return None
    try:
        runpod_id = await ctx.runpod.dispatch(
            job_id=job.id,
            idempotency_key=dispatch_key,
            payload=payload,
        )
    except StageError:
        # The reservation remains outstanding. Even a timeout or 5xx can mean
        # RunPod accepted the request, so reconciliation never redispatches it.
        raise
    await _confirm_dispatch(
        ctx,
        idempotency_key=dispatch_key,
        runpod_job_id=runpod_id,
    )
    return None


def _confirmed_dispatch_key(events: list[Any], runpod_job_id: str | None) -> str | None:
    """Find the idempotency key bound to the currently polled RunPod job."""
    for event in reversed(events):
        detail = event.detail if isinstance(event.detail, dict) else {}
        if (
            event.event == "runpod_dispatched"
            and detail.get("runpod_job_id") == runpod_job_id
            and isinstance(detail.get("idempotency_key"), str)
        ):
            return detail["idempotency_key"]
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
    if ctx.settings.cloud_pipeline:
        # Cloud jobs never enter `splitting` (downloading hands off to
        # `dispatched`); a job here means the pipeline profile switched
        # mid-flight. Running the local splitter would feed cloud
        # verification a package shape it cannot prove, so fail clearly and
        # terminally instead of burning retries.
        raise StageError(
            ErrorCode.INTERNAL,
            "job reached splitting while the cloud pipeline is selected "
            "(pipeline profile switched mid-flight); re-upload the source",
            retryable=False,
        )
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
    path: reuse the on-disk pipeline describe + a local placeholder prefix —
    which publish_track REFUSES (invariant C7): a local/ row has no object in
    S3 and can never play, so the job fails non-retryably with PUBLISH_FAILED
    and a publish_refused event instead of creating a phantom library row.
    """
    if ctx.settings.cloud_pipeline:
        return await cloud.cloud_publishing(ctx)
    manifest = await ctx.pipeline.describe(ctx.job_dir)
    local_prefix = f"local/{ctx.job.id.hex}"
    try:
        await ctx.jobs.publish_track(
            ctx.job.id,
            worker_id=ctx.worker_id,
            title=manifest.get("title") or ctx.job.title or ctx.job.id.hex,
            artist=manifest.get("artist") or ctx.job.artist or "",
            duration_seconds=float(manifest.get("duration", 0.0)),
            s3_prefix=local_prefix,
            manifest_key=f"{local_prefix}/stems.json",
            generation=1,
            integrity=manifest.get("integrity"),
        )
    except StageError:
        raise
    except InvalidTransition:
        # Lost lease: the loop's InvalidTransition handling yields the job.
        raise
    except PublishRefusedError as e:
        # Deterministic: re-running cannot change the refused location.
        raise StageError(ErrorCode.PUBLISH_FAILED, str(e)[:500], retryable=False) from e
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

"""Unit: orchestrator loop mechanics on SQLite (claims, leases, retries,
duplicate completion). Real-Postgres concurrency lives in tests/contract/."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from shizzle_server.db.models import JobStage, SourceType, utcnow
from shizzle_server.db.repository import track_id_for_job
from shizzle_server.orchestrator.loop import Orchestrator


async def wait_for_status(repo, job_id, status: JobStage, timeout: float = 10.0):
    async def _poll():
        while True:
            job = await repo.get_job(job_id)
            if job is not None and job.status == status:
                return job
            await asyncio.sleep(0.05)

    return await asyncio.wait_for(_poll(), timeout=timeout)


async def run_orchestrator_until(settings, predicate_coro):
    """Run an in-process orchestrator until predicate_coro() completes."""
    orch = Orchestrator(settings, worker_id="unit-worker")
    task = asyncio.create_task(orch.run_forever())
    try:
        return await predicate_coro
    finally:
        orch.request_stop()
        await asyncio.wait_for(task, timeout=10)


async def test_upload_job_runs_to_ready_and_publishes_track(
    settings, job_repo, track_repo, upload_job
):
    job = await run_orchestrator_until(
        settings, wait_for_status(job_repo, upload_job.id, JobStage.ready)
    )
    assert job.error_code is None
    assert job.track_id == track_id_for_job(upload_job.id)
    # Stage timings recorded for every executed stage
    assert set(job.stage_timings) >= {"pending", "downloading", "splitting", "verifying"}
    # Track row exists with the local placeholder prefix, generation 1
    track = await track_repo.get(job.track_id)
    assert track is not None
    assert track.generation == 1
    assert track.s3_prefix == f"local/{upload_job.id.hex}"
    # Event history is unbroken: created .. stage_completed*4 .. track_published
    events = [e.event for e in await job_repo.list_events(upload_job.id)]
    assert events[0] == "created"
    assert events.count("stage_completed") == 4
    assert "track_published" in events


async def test_url_job_fails_with_structured_ytdlp_blocked(settings, job_repo):
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://youtube.com/x")
    failed = await run_orchestrator_until(
        settings, wait_for_status(job_repo, job.id, JobStage.failed)
    )
    assert failed.error_code == "YTDLP_BLOCKED"
    assert failed.attempt == 0  # non-retryable: failed immediately, no retries
    events = [e.event for e in await job_repo.list_events(job.id)]
    assert "failed" in events and "retry_scheduled" not in events


async def test_retry_schedule_honored_then_succeeds(settings, job_repo, upload_job):
    settings.shizzle_test_fail_times = 2  # two injected retryable failures
    job = await run_orchestrator_until(
        settings, wait_for_status(job_repo, upload_job.id, JobStage.ready, timeout=20)
    )
    assert job.attempt == 2
    events = await job_repo.list_events(upload_job.id)
    retries = [e for e in events if e.event == "retry_scheduled"]
    assert [e.detail["attempt"] for e in retries] == [1, 2]
    # Exponential backoff: second delay is double the first
    assert retries[1].detail["retry_in_seconds"] == 2 * retries[0].detail["retry_in_seconds"]


async def test_max_attempts_exhausted_fails_with_code(settings, job_repo, upload_job):
    settings.shizzle_test_fail_times = 99  # never stops failing
    failed = await run_orchestrator_until(
        settings, wait_for_status(job_repo, upload_job.id, JobStage.failed, timeout=20)
    )
    assert failed.error_code == "DEMUCS_FAILED"
    assert "max attempts" in failed.error_detail
    # attempts: max_attempts-1 retries scheduled, then terminal failure
    events = [e.event for e in await job_repo.list_events(upload_job.id)]
    assert events.count("retry_scheduled") == settings.orchestrator_max_attempts - 1
    assert events.count("failed") == 1


async def test_claim_respects_active_lease_and_retry_time(job_repo, upload_job):
    now = utcnow()
    # Job claimed by worker A with a live lease -> worker B gets nothing
    claimed = await job_repo.claim_next(worker_id="A", lease_seconds=120)
    assert claimed is not None and claimed.lease_owner == "A"
    assert await job_repo.claim_next(worker_id="B", lease_seconds=120) is None

    # Expired lease -> reclaimable, and the reclaim is recorded
    expired = now - timedelta(seconds=1)

    async with job_repo._sf() as session, session.begin():
        from shizzle_server.db.models import Job

        row = await session.get(Job, upload_job.id)
        row.lease_expires_at = expired

    reclaimed = await job_repo.claim_next(worker_id="B", lease_seconds=120)
    assert reclaimed is not None and reclaimed.lease_owner == "B"
    events = [e.event for e in await job_repo.list_events(upload_job.id)]
    assert "lease_reclaimed" in events

    # next_retry_at in the future -> not claimable
    await job_repo.release_lease(upload_job.id, worker_id="B")
    async with job_repo._sf() as session, session.begin():
        from shizzle_server.db.models import Job

        row = await session.get(Job, upload_job.id)
        row.next_retry_at = utcnow() + timedelta(seconds=60)
    assert await job_repo.claim_next(worker_id="B", lease_seconds=120) is None


async def test_renew_and_release_lease_ownership(job_repo, upload_job):
    await job_repo.claim_next(worker_id="A", lease_seconds=5)
    assert await job_repo.renew_lease(upload_job.id, worker_id="A", lease_seconds=5) is True
    assert await job_repo.renew_lease(upload_job.id, worker_id="B", lease_seconds=5) is False
    await job_repo.release_lease(upload_job.id, worker_id="A")
    job = await job_repo.get_job(upload_job.id)
    assert job.lease_owner is None and job.lease_expires_at is None


async def test_duplicate_completion_is_noop(job_repo, upload_job):
    for a, b in [
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.splitting),
        (JobStage.splitting, JobStage.verifying),
        (JobStage.verifying, JobStage.publishing),
    ]:
        await job_repo.advance(upload_job.id, from_stage=a, to_stage=b)

    kwargs = {
        "title": "T",
        "duration_seconds": 1.0,
        "s3_prefix": f"local/{upload_job.id.hex}",
        "manifest_key": f"local/{upload_job.id.hex}/stems.json",
    }
    first = await job_repo.publish_track(upload_job.id, **kwargs)
    second = await job_repo.publish_track(upload_job.id, **kwargs)  # duplicate completion
    assert first.id == second.id

    events = [e.event for e in await job_repo.list_events(upload_job.id)]
    assert events.count("track_published") == 1
    assert events.count("duplicate_completion_ignored") == 1

    job = await job_repo.get_job(upload_job.id)
    assert job.status == JobStage.ready


async def test_dispatched_stage_not_claimed_by_loop(job_repo):
    """dispatched belongs to the Phase 3 webhook/reconciler, not the lease loop."""
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://x")
    await job_repo.advance(job.id, from_stage=JobStage.pending, to_stage=JobStage.downloading)
    await job_repo.advance(job.id, from_stage=JobStage.downloading, to_stage=JobStage.dispatched)
    assert await job_repo.claim_next(worker_id="A", lease_seconds=120) is None


async def test_orchestrator_heartbeat_written(settings, heartbeat_repo, job_repo):
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://x")
    await run_orchestrator_until(
        settings, wait_for_status(job_repo, job.id, JobStage.failed)
    )
    assert await heartbeat_repo.latest() is not None


async def test_effect_counter_stage_idempotency(settings, job_repo, upload_job):
    """The test pipeline's marker files mirror real on-disk idempotency:
    a re-run of split after completion must not repeat the effects."""
    await run_orchestrator_until(
        settings, wait_for_status(job_repo, upload_job.id, JobStage.ready)
    )
    effects_log = settings.data_dir / upload_job.id.hex / "effects.log"
    effects = effects_log.read_text().splitlines()
    job_effects = [line.split()[1] for line in effects if line.startswith(upload_job.id.hex)]
    for sub in ("extracting", "splitting", "encoding"):
        assert job_effects.count(sub) == 1

    # Re-run split directly: markers exist, so zero new effects.
    from shizzle_server.orchestrator.pipelines import TestPipeline

    pipeline = TestPipeline(settings)
    await pipeline.split(settings.data_dir / upload_job.id.hex, Path("source.mp4"), "T")
    effects_after = effects_log.read_text().splitlines()
    assert len(effects_after) == len(effects)

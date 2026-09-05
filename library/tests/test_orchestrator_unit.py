"""Unit: orchestrator loop mechanics on SQLite (claims, leases, retries,
duplicate completion). Real-Postgres concurrency lives in tests/contract/."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from shizzle_server.api.media import _s3_client
from shizzle_server.db.models import Job, JobStage, SourceType, utcnow
from shizzle_server.db.repository import track_id_for_job
from shizzle_server.errors import ErrorCode, StageError
from shizzle_server.orchestrator import cloud
from shizzle_server.orchestrator.loop import Orchestrator
from shizzle_server.orchestrator.stages import (
    StageContext,
    _age_seconds,
    handle_dispatched,
    handle_splitting,
)
from shizzle_server.settings import Settings


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


async def test_upload_job_reaches_publishing_then_is_refused(
    settings, job_repo, track_repo, upload_job
):
    """The test pipeline sails through the stages, then the C7 publish guard
    refuses the local/ placeholder row: no track, job failed non-retryably."""
    job = await run_orchestrator_until(
        settings, wait_for_status(job_repo, upload_job.id, JobStage.failed)
    )
    assert job.error_code == "PUBLISH_FAILED"
    assert job.attempt == 0
    assert job.track_id is None
    # Stage timings recorded for every executed stage
    assert set(job.stage_timings) >= {"pending", "downloading", "splitting", "verifying"}
    # No phantom track row
    assert await track_repo.get(track_id_for_job(upload_job.id)) is None
    assert await track_repo.list_tracks() == []
    # Event history is unbroken: created .. stage_completed*4 .. publish_refused, failed
    events = [e.event for e in await job_repo.list_events(upload_job.id)]
    assert events[0] == "created"
    assert events.count("stage_completed") == 4
    assert "publish_refused" in events
    assert "track_published" not in events


async def test_url_job_fails_with_structured_ytdlp_blocked(settings, job_repo):
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://youtube.com/x")
    failed = await run_orchestrator_until(
        settings, wait_for_status(job_repo, job.id, JobStage.failed)
    )
    assert failed.error_code == "YTDLP_BLOCKED"
    assert failed.attempt == 0  # non-retryable: failed immediately, no retries
    events = [e.event for e in await job_repo.list_events(job.id)]
    assert "failed" in events and "retry_scheduled" not in events


async def test_retry_schedule_honored_until_publish_refusal(settings, job_repo, upload_job):
    settings.shizzle_test_fail_times = 2  # two injected retryable failures in split
    job = await run_orchestrator_until(
        settings, wait_for_status(job_repo, upload_job.id, JobStage.failed, timeout=20)
    )
    assert job.attempt == 2
    # Splitting recovered after the scheduled retries; publishing was then
    # refused non-retryably (C7).
    assert job.error_code == "PUBLISH_FAILED"
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


async def test_stage_error_yields_when_lease_was_lost(settings, job_repo, upload_job):
    """Issue #19: after a reclaim, the evicted worker's error handling must
    not fail or retry the job — _handle_stage_error treats InvalidTransition
    as "lease lost" and stops applying effects (B2)."""
    orch = Orchestrator(settings, worker_id="stale-loop")
    try:
        claimed = await job_repo.claim_next(worker_id="stale-loop", lease_seconds=60)
        assert claimed is not None and claimed.id == upload_job.id
        await job_repo.advance(
            upload_job.id,
            from_stage=JobStage.pending,
            to_stage=JobStage.downloading,
            worker_id="stale-loop",
        )
        await job_repo.advance(
            upload_job.id,
            from_stage=JobStage.downloading,
            to_stage=JobStage.splitting,
            worker_id="stale-loop",
        )
        # A peer reclaims the expired lease out from under the loop.
        async with job_repo._sf() as session, session.begin():
            row = await session.get(Job, upload_job.id)
            row.lease_expires_at = utcnow() - timedelta(seconds=1)
        reclaimed = await job_repo.claim_next(worker_id="peer", lease_seconds=60)
        assert reclaimed is not None and reclaimed.lease_owner == "peer"

        job = await job_repo.get_job(upload_job.id)
        await orch._handle_stage_error(
            job,
            JobStage.splitting,
            StageError(ErrorCode.DEMUCS_FAILED, "boom", retryable=True),
        )
        await orch._handle_stage_error(
            job,
            JobStage.splitting,
            StageError(ErrorCode.INTERNAL, "fatal", retryable=False),
        )

        survived = await job_repo.get_job(upload_job.id)
        assert survived.status is JobStage.splitting
        assert survived.lease_owner == "peer"
        assert survived.attempt == 0
        assert survived.error_code is None
    finally:
        await orch.engine.dispose()


async def test_duplicate_completion_is_noop(job_repo, upload_job):
    claimed = await job_repo.claim_next(worker_id="dup-test", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    for a, b in [
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.splitting),
        (JobStage.splitting, JobStage.verifying),
        (JobStage.verifying, JobStage.publishing),
    ]:
        await job_repo.advance(upload_job.id, from_stage=a, to_stage=b, worker_id="dup-test")

    kwargs = {
        "title": "T",
        "duration_seconds": 1.0,
        "s3_prefix": f"tracks/{track_id_for_job(upload_job.id)}/1",
        "manifest_key": f"tracks/{track_id_for_job(upload_job.id)}/1/manifest.json",
        "worker_id": "dup-test",
    }
    first = await job_repo.publish_track(upload_job.id, **kwargs)
    second = await job_repo.publish_track(upload_job.id, **kwargs)  # duplicate completion
    assert first.id == second.id

    events = [e.event for e in await job_repo.list_events(upload_job.id)]
    assert events.count("track_published") == 1
    # No duplicate_completion_ignored event: a ready job holds no lease, so a
    # completion arriving after ready can only be a stale writer, and the
    # duplicate path is a no-write no-op (B2).
    assert "duplicate_completion_ignored" not in events

    job = await job_repo.get_job(upload_job.id)
    assert job.status == JobStage.ready


async def test_dispatched_stage_is_claimed_by_loop(job_repo):
    """``dispatched`` is owned by the claim-based RunPod reconciler (WS4): the
    lease loop claims it and handle_dispatched parks between polls."""
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://x")
    claimed = await job_repo.claim_next(worker_id="A", lease_seconds=120)
    assert claimed is not None and claimed.id == job.id
    await job_repo.advance(
        job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="A",
    )
    await job_repo.advance(
        job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="A",
    )
    await job_repo.release_lease(job.id, worker_id="A")
    reclaimed = await job_repo.claim_next(worker_id="A", lease_seconds=120)
    assert reclaimed is not None and reclaimed.id == job.id
    assert reclaimed.status == JobStage.dispatched


async def test_orchestrator_heartbeat_written(settings, heartbeat_repo, job_repo):
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://x")
    await run_orchestrator_until(
        settings, wait_for_status(job_repo, job.id, JobStage.failed)
    )
    assert await heartbeat_repo.latest() is not None


async def test_effect_counter_stage_idempotency(settings, job_repo, upload_job):
    """The test pipeline's marker files mirror real on-disk idempotency:
    a re-run of split after completion must not repeat the effects."""
    # Publishing is refused (C7), but every stage before it ran exactly once.
    await run_orchestrator_until(
        settings, wait_for_status(job_repo, upload_job.id, JobStage.failed)
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


# --- WS1: cloud source upload (moto) -----------------------------------------
# B2-independent: upload_source needs only s3_client + track_id_for_job, both of
# which already exist. The handle_downloading cloud fork that invokes it lands
# with WS4 (needs Settings.cloud_pipeline from B2).


@pytest.fixture
def fake_aws_credentials(monkeypatch):
    for var, value in {
        "AWS_ACCESS_KEY_ID": "testing" * 5,
        "AWS_SECRET_ACCESS_KEY": "testing" * 6,
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(var, value)
    for var in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)


def _ctx_for_upload(settings: Settings, tmp_path: Path) -> StageContext:
    job_id = uuid.uuid4()
    job_dir = settings.data_dir / job_id.hex
    job_dir.mkdir(parents=True)
    (job_dir / "source.mp4").write_bytes(b"cloud source bytes")
    job = Job(
        id=job_id, source_type=SourceType.upload, source_ref="source.mp4",
        idempotency_key="k", profile_version=1,
    )
    return StageContext(
        job=job, settings=settings, pipeline=None, jobs=None, runpod=None,  # type: ignore[arg-type]
        worker_id="upload-test",
    )


async def test_upload_source_puts_source_at_deterministic_key(fake_aws_credentials, tmp_path, monkeypatch):
    _s3_client.cache_clear()
    bucket = "shizzle-upload-test"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings = Settings(
        data_dir=data_dir,
        s3_media_bucket=bucket,
        aws_region="us-east-1",
        aws_endpoint_url="",
    )
    ctx = _ctx_for_upload(settings, tmp_path)
    track_id = track_id_for_job(ctx.job.id)
    expected_key = cloud.source_key(track_id)

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=bucket)
        key = await cloud.upload_source(ctx)

        assert key == expected_key
        head = s3.head_object(Bucket=bucket, Key=expected_key)
        assert head["ContentLength"] == len(b"cloud source bytes")
        assert head["ContentType"] == "video/mp4"


async def test_upload_source_is_idempotent_on_size_match(fake_aws_credentials, tmp_path, monkeypatch):
    """A second call with an unchanged source skips upload_file entirely
    (head size matches). A size change after the first upload forces a re-upload."""
    _s3_client.cache_clear()
    bucket = "shizzle-upload-test"
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    settings = Settings(
        data_dir=data_dir, s3_media_bucket=bucket, aws_region="us-east-1", aws_endpoint_url=""
    )
    ctx = _ctx_for_upload(settings, tmp_path)

    state = {"inner": None, "uploads": 0}

    def _fake_s3_client(_settings):
        if state["inner"] is None:
            state["inner"] = boto3.client("s3", region_name="us-east-1")
        inner = state["inner"]

        class _Rec:
            def __getattr__(self, name):
                attr = getattr(inner, name)
                if not callable(attr):
                    return attr

                def wrapped(*args, **kwargs):
                    if name == "upload_file":
                        state["uploads"] += 1
                    return attr(*args, **kwargs)

                return wrapped

        return _Rec()

    monkeypatch.setattr(cloud, "s3_client", _fake_s3_client)

    with mock_aws():
        cloud.s3_client(settings).create_bucket(Bucket=bucket)  # type: ignore[attr-defined]
        await cloud.upload_source(ctx)
        assert state["uploads"] == 1
        await cloud.upload_source(ctx)  # same size -> head matches -> skip
        assert state["uploads"] == 1
        # Mutate the local source size -> head mismatch -> re-upload.
        ctx.source_path.write_bytes(b"changed length source bytes!!")
        await cloud.upload_source(ctx)
        assert state["uploads"] == 2


async def test_accepted_dispatch_id_reads_matching_worker_receipt(
    settings, monkeypatch
):
    ctx = _ctx_for_upload(settings, settings.data_dir)
    track_id = track_id_for_job(ctx.job.id)
    dispatch_key = f"{ctx.job.id.hex}:0"
    receipt = {
        "runpod_job_id": "rp-from-worker",
        "idempotency_key": dispatch_key,
        "track_id": str(track_id),
        "generation": cloud.GENERATION,
        "package_prefix": cloud.attempt_prefix(track_id, dispatch_key),
    }

    class Body:
        def read(self):
            return json.dumps(receipt).encode()

    class S3:
        def get_object(self, *, Bucket, Key):  # noqa: ANN001, N803
            assert Bucket == settings.s3_media_bucket
            assert Key == cloud.dispatch_receipt_key(track_id, dispatch_key)
            return {"Body": Body()}

    monkeypatch.setattr(cloud, "s3_client", lambda _settings: S3())
    assert (
        await cloud.accepted_dispatch_id(ctx, idempotency_key=dispatch_key)
        == "rp-from-worker"
    )

    receipt["idempotency_key"] = "stale-attempt"
    assert await cloud.accepted_dispatch_id(ctx, idempotency_key=dispatch_key) is None


async def test_accepted_dispatch_id_reads_legacy_base_receipt(settings, monkeypatch):
    ctx = _ctx_for_upload(settings, settings.data_dir)
    track_id = track_id_for_job(ctx.job.id)
    receipt = {
        "runpod_job_id": "rp-legacy",
        "track_id": str(track_id),
        "generation": cloud.GENERATION,
    }

    class Body:
        def read(self):
            return json.dumps(receipt).encode()

    class S3:
        def get_object(self, *, Bucket, Key):  # noqa: ANN001, N803
            assert Bucket == settings.s3_media_bucket
            assert Key == f"{cloud.separation_prefix(track_id)}/dispatch.json"
            return {"Body": Body()}

    monkeypatch.setattr(cloud, "s3_client", lambda _settings: S3())
    assert await cloud.accepted_dispatch_id(ctx, idempotency_key=None) == "rp-legacy"


# --- WS4: dispatched reconciliation (FakeRunPodClient) ------------------------
# These exercise handle_dispatched through the real loop: park-on-None, phase
# recording on change, queue/stall watchdogs, and fresh re-dispatch after a
# terminal failure. cloud_verifying/publishing are stubbed (ffmpeg lives in the
# WS5 integration gate); upload_source is stubbed so no S3 is needed.


class FakeRunPodClient:
    """Scripted RunPod client. ``poll`` returns scripted responses in order,
    repeating the last one when the script is exhausted (keeps stall/queue
    watchdog tests deterministic)."""

    def __init__(
        self,
        responses: list[dict | Exception],
        *,
        cancel_error: Exception | None = None,
    ) -> None:
        self.responses = list(responses)
        self._i = 0
        self.dispatched: list[tuple[str, str, dict]] = []
        self.polled: list[str] = []
        self.cancelled: list[str] = []
        self.cancel_error = cancel_error

    async def dispatch(self, *, job_id, idempotency_key, payload):
        rid = f"rp-{len(self.dispatched) + 1}"
        self.dispatched.append((rid, idempotency_key, payload))
        return rid

    async def poll(self, runpod_job_id: str) -> dict:
        self.polled.append(runpod_job_id)
        resp = self.responses[min(self._i, len(self.responses) - 1)]
        self._i += 1
        if isinstance(resp, Exception):
            raise resp
        return resp

    async def cancel(self, runpod_job_id: str) -> None:
        self.cancelled.append(runpod_job_id)
        if self.cancel_error is not None:
            raise self.cancel_error


def _stub_cloud_intake(monkeypatch) -> None:
    """Bypass S3 + ffmpeg: upload_source no-ops, verifying/publishing advance."""
    async def _upload(ctx):
        return cloud.source_key(track_id_for_job(ctx.job.id))

    async def _verify(ctx):
        ctx.detail["package"] = {"duration": 1.0, "sample_count": 44100}
        return JobStage.publishing

    async def _publish(ctx):
        tid = track_id_for_job(ctx.job.id)
        await ctx.jobs.publish_track(
            ctx.job.id,
            worker_id=ctx.worker_id,
            title=ctx.job.title or ctx.job.id.hex,
            artist="",
            duration_seconds=1.0, s3_prefix=f"tracks/{tid}/1",
            manifest_key=f"tracks/{tid}/1/manifest.json", generation=1, integrity={},
        )
        ctx.published = True
        return JobStage.ready

    monkeypatch.setattr(cloud, "upload_source", _upload)
    monkeypatch.setattr(cloud, "cloud_verifying", _verify)
    monkeypatch.setattr(cloud, "cloud_publishing", _publish)


async def _run_cloud_orch(settings, fake: FakeRunPodClient):
    settings.shizzle_pipeline = "cloud"
    # Placeholder credentials so the orchestrator skips the parked-cloud
    # warning path; the fake client replaces the HTTP one below.
    settings.runpod_api_key = "unit-test-key"
    settings.runpod_endpoint_id = "unit-test-endpoint"
    orch = Orchestrator(settings, worker_id="cloud-unit")
    orch.runpod = fake
    task = asyncio.create_task(orch.run_forever())
    return orch, task


def test_cloud_orchestrator_parked_without_runpod_config(settings, caplog):
    """Cloud mode without RunPod credentials is the valid parked state: the
    orchestrator still starts (its heartbeat feeds /api/health) and logs one
    WARNING; new jobs fail closed at dispatch."""
    settings.shizzle_pipeline = "cloud"
    settings.runpod_api_key = ""
    settings.runpod_endpoint_id = ""
    with caplog.at_level(logging.WARNING, logger="shizzle_server.orchestrator.loop"):
        orch = Orchestrator(settings, worker_id="cloud-parked")
    assert any(
        record.levelno == logging.WARNING
        and "RunPod not configured" in record.message
        and "RUNPOD_DISPATCH_FAILED" in record.message
        for record in caplog.records
    )
    from shizzle_server.orchestrator.runpod_client import NotConfiguredRunPodClient

    assert isinstance(orch.runpod, NotConfiguredRunPodClient)

    # Configured cloud mode starts without the warning.
    caplog.clear()
    settings.runpod_api_key = "key"
    settings.runpod_endpoint_id = "endpoint"
    with caplog.at_level(logging.WARNING, logger="shizzle_server.orchestrator.loop"):
        Orchestrator(settings, worker_id="cloud-configured")
    assert not caplog.records


async def test_not_configured_runpod_client_reports_dispatch_failure():
    """If a cloud handler is still reached unconfigured, a fresh dispatch
    fails with RUNPOD_DISPATCH_FAILED (non-retryable), not a generic
    INTERNAL."""
    from shizzle_server.orchestrator.runpod_client import NotConfiguredRunPodClient

    client = NotConfiguredRunPodClient()
    with pytest.raises(StageError) as excinfo:
        await client.dispatch(job_id=uuid.uuid4(), idempotency_key="k", payload={})
    assert excinfo.value.code == ErrorCode.RUNPOD_DISPATCH_FAILED
    assert excinfo.value.retryable is False
    assert "RUNPOD_API_KEY" in excinfo.value.detail

    # Polling an already-dispatched remote job stays retryable: the failure
    # says nothing about that job, so the handler parks it until credentials
    # return instead of failing it permanently.
    with pytest.raises(StageError) as poll_exc:
        await client.poll("rp-parked")
    assert poll_exc.value.code == ErrorCode.RUNPOD_DISPATCH_FAILED
    assert poll_exc.value.retryable is True


async def test_parked_cloud_parks_inflight_dispatched_job(
    settings, job_repo, session_factory, upload_job
):
    """A job dispatched before RunPod credentials vanished must park, not
    fail: with the unconfigured client the poll failure carries no signal
    about the remote job, so even a heartbeat past the stall window must not
    trip the stall watchdog."""
    from shizzle_server.orchestrator.runpod_client import NotConfiguredRunPodClient

    settings.shizzle_pipeline = "cloud"
    settings.runpod_worker_stall_seconds = 300.0
    claimed = await job_repo.claim_next(worker_id="parked", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="parked",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="parked",
    )
    async with session_factory() as session, session.begin():
        job = await session.get(Job, upload_job.id)
        job.runpod_job_id = "rp-parked"
        job.worker_phase = "extracting"
        # Stale past the stall window: would be killed on a real poll outage.
        job.worker_heartbeat_at = utcnow() - timedelta(seconds=3600)

    ctx = StageContext(
        job=await job_repo.get_job(upload_job.id),
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=NotConfiguredRunPodClient(),
        worker_id="parked",
    )
    assert await handle_dispatched(ctx) is None

    job = await job_repo.get_job(upload_job.id)
    assert job.status == JobStage.dispatched  # parked, not failed
    assert job.worker_phase == "extracting"  # not marked failed


async def test_cloud_pipeline_fails_inflight_splitting_job(settings, job_repo, upload_job):
    """A job that entered `splitting` before a mid-flight switch to the cloud
    profile fails clearly and terminally instead of running the local splitter
    into cloud verification."""
    settings.shizzle_pipeline = "cloud"
    settings.runpod_api_key = "unit-test-key"
    settings.runpod_endpoint_id = "unit-test-endpoint"
    claimed = await job_repo.claim_next(worker_id="profile-switch", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    for a, b in [
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.splitting),
    ]:
        await job_repo.advance(upload_job.id, from_stage=a, to_stage=b, worker_id="profile-switch")

    ctx = StageContext(
        job=await job_repo.get_job(upload_job.id),
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=None,  # type: ignore[arg-type]
        worker_id="profile-switch",
    )
    with pytest.raises(StageError) as excinfo:
        await handle_splitting(ctx)
    assert excinfo.value.retryable is False
    assert "profile switched mid-flight" in excinfo.value.detail


async def test_cloud_dispatched_e2e_records_progress_and_completes(
    settings, job_repo, track_repo, upload_job, monkeypatch
):
    """upload -> dispatched -> IN_PROGRESS phases recorded on change ->
    COMPLETED -> verifying -> publishing -> ready, track linked."""
    _stub_cloud_intake(monkeypatch)
    settings.runpod_poll_seconds = 0.02
    fake = FakeRunPodClient([
        {"status": "IN_PROGRESS", "output": {"phase": "extracting"}},
        {"status": "IN_PROGRESS", "output": {"phase": "encoding"}},
        {"status": "COMPLETED", "output": {"separation": {"sample_count": 44100}}},
    ])
    orch, task = await _run_cloud_orch(settings, fake)
    try:
        job = await wait_for_status(job_repo, upload_job.id, JobStage.ready, timeout=15)
    finally:
        orch.request_stop()
        await asyncio.wait_for(task, timeout=10)

    assert len(fake.dispatched) == 1
    assert fake.dispatched[0][1] == f"{upload_job.id.hex}:0"  # per-attempt idempotency key
    assert job.runpod_job_id == fake.dispatched[0][0]
    assert job.track_id == track_id_for_job(upload_job.id)
    events = await job_repo.list_events(upload_job.id)
    # Phases recorded only on change (dispatched seed -> extracting -> encoding).
    progress = [e.detail["phase"] for e in events if e.event == "worker_progress"]
    assert progress == ["extracting", "encoding"]
    assert any(e.event == "worker_completed" for e in events)
    assert job.track_id is not None
    assert (await track_repo.get(job.track_id)) is not None


async def test_cloud_dispatched_stable_phase_remains_live(
    settings, job_repo, upload_job, monkeypatch
):
    """Repeated successful polls in one phase refresh liveness without events."""
    _stub_cloud_intake(monkeypatch)
    settings.runpod_poll_seconds = 0.02
    # The stall watchdog must never fire here: a 1.0 s threshold is reachable
    # by a slow CI runner between polls (master flaked on exactly that), and
    # 30 s exceeds the 15 s wait timeout below, so the watchdog can only fire
    # in a run that has already failed.
    settings.runpod_worker_stall_seconds = 30.0
    fake = FakeRunPodClient([{"status": "IN_PROGRESS", "output": {"phase": "working"}}])
    orch, task = await _run_cloud_orch(settings, fake)
    try:
        async def _polled_repeatedly():
            return len(fake.polled) >= 3 or None

        await asyncio.wait_for(_poll_predicate(_polled_repeatedly), timeout=15)
    finally:
        orch.request_stop()
        await asyncio.wait_for(task, timeout=10)

    assert fake.cancelled == []
    job = await job_repo.get_job(upload_job.id)
    assert job.attempt == 0
    assert job.worker_phase == "working"
    assert job.error_code is None


@pytest.mark.parametrize("superseded", [False, True])
async def test_cloud_dispatched_runpod_failed_redispatches_fresh(
    settings, job_repo, upload_job, monkeypatch, superseded
):
    """A terminal RunPod failure marks the runpod id dead; the retry dispatches
    a fresh job under a new idempotency key and still reaches ready."""
    _stub_cloud_intake(monkeypatch)
    settings.runpod_poll_seconds = 0.02

    async def package_not_ready(_ctx, *, idempotency_key):
        assert idempotency_key == f"{upload_job.id.hex}:0"
        return False

    monkeypatch.setattr(cloud, "package_ready", package_not_ready)
    fake = FakeRunPodClient([
        {"status": "COMPLETED", "output": {"status": "SUPERSEDED"}}
        if superseded else {"status": "FAILED", "output": {"error": "boom"}},
        {"status": "IN_PROGRESS", "output": {"phase": "ok"}},
        {"status": "COMPLETED", "output": {}},
    ])
    orch, task = await _run_cloud_orch(settings, fake)
    try:
        await wait_for_status(job_repo, upload_job.id, JobStage.ready, timeout=15)
    finally:
        orch.request_stop()
        await asyncio.wait_for(task, timeout=10)

    assert len(fake.dispatched) == 2
    rp1, rp2 = fake.dispatched[0][0], fake.dispatched[1][0]
    assert rp1 != rp2
    assert fake.dispatched[0][1] == f"{upload_job.id.hex}:0"
    assert fake.dispatched[1][1] == f"{upload_job.id.hex}:1"
    job = await job_repo.get_job(upload_job.id)
    assert job.runpod_job_id == rp2  # moved off the dead id
    events = await job_repo.list_events(upload_job.id)
    completed = [event for event in events if event.event == "worker_completed"]
    assert len(completed) == 1
    assert completed[0].detail == {}
    verification = [
        event for event in events
        if event.event == "stage_completed" and event.detail.get("to") == "verifying"
    ]
    assert len(verification) == 1
    assert verification[0].detail["package_prefix"] == cloud.attempt_prefix(
        track_id_for_job(upload_job.id), fake.dispatched[1][1]
    )


async def test_cloud_dispatched_park_does_not_increment_attempt(
    settings, job_repo, upload_job, monkeypatch
):
    """An IN_QUEUE job parks between polls: status stays dispatched, attempt
    untouched, exactly one dispatch under the attempt-0 idempotency key."""
    _stub_cloud_intake(monkeypatch)
    settings.runpod_poll_seconds = 0.02
    fake = FakeRunPodClient([{"status": "IN_QUEUE", "output": {}}])
    orch, task = await _run_cloud_orch(settings, fake)
    try:
        async def _polled_repeatedly():
            return len(fake.polled) >= 3 or None

        await asyncio.wait_for(_poll_predicate(_polled_repeatedly), timeout=15)
    finally:
        orch.request_stop()
        await asyncio.wait_for(task, timeout=10)

    job = await job_repo.get_job(upload_job.id)
    assert job.status == JobStage.dispatched
    assert job.attempt == 0
    assert job.runpod_job_id is not None
    assert len(fake.dispatched) == 1  # parked, never re-dispatched


async def test_queue_watchdog_age_survives_two_parks(job_repo, upload_job):
    claimed = await job_repo.claim_next(worker_id="queue-test", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="queue-test",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="queue-test",
    )
    await job_repo.record_dispatch(
        upload_job.id, worker_id="queue-test", runpod_job_id="rp-queue"
    )
    await job_repo.record_worker_progress(
        upload_job.id, phase="queued", worker_id="queue-test"
    )

    await job_repo.park(
        upload_job.id, worker_id="queue-test", recheck_in_seconds=0
    )
    first = await job_repo.get_job(upload_job.id)
    first_age = _age_seconds(first.worker_heartbeat_at)
    await asyncio.sleep(0.02)

    await job_repo.claim_next(worker_id="queue-test", lease_seconds=60)
    assert (
        await job_repo.record_worker_progress(
            upload_job.id, phase="queued", worker_id="queue-test"
        )
        is False
    )
    await job_repo.park(
        upload_job.id, worker_id="queue-test", recheck_in_seconds=0
    )
    second = await job_repo.get_job(upload_job.id)
    second_age = _age_seconds(second.worker_heartbeat_at)

    assert first_age is not None and second_age is not None
    assert second_age > first_age
    assert second.updated_at > second.worker_heartbeat_at


async def test_transient_poll_failure_parks_without_consuming_attempt(
    settings, job_repo, upload_job
):
    claimed = await job_repo.claim_next(worker_id="poll-test", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="poll-test",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="poll-test",
    )
    await job_repo.record_dispatch(
        upload_job.id, worker_id="poll-test", runpod_job_id="rp-poll"
    )
    job = await job_repo.get_job(upload_job.id)
    fake = FakeRunPodClient([
        StageError(ErrorCode.RUNPOD_TIMEOUT, "temporary", retryable=True)
    ])
    ctx = StageContext(
        job=job,
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=fake,
        worker_id="poll-test",
    )

    assert await handle_dispatched(ctx) is None
    assert (await job_repo.get_job(upload_job.id)).attempt == 0
    failures = [
        event for event in await job_repo.list_events(upload_job.id)
        if event.event == "runpod_poll_failed"
    ]
    assert failures[0].detail["error_code"] == "RUNPOD_TIMEOUT"


async def test_stale_poll_outage_cancels_and_marks_existing_job_failed(
    settings, job_repo, upload_job
):
    claimed = await job_repo.claim_next(worker_id="poll-test", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="poll-test",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="poll-test",
    )
    await job_repo.record_dispatch(
        upload_job.id, worker_id="poll-test", runpod_job_id="rp-stale"
    )
    settings.runpod_worker_stall_seconds = 10
    async with job_repo._sf() as session, session.begin():
        row = await session.get(Job, upload_job.id)
        row.worker_heartbeat_at = utcnow() - timedelta(seconds=11)

    fake = FakeRunPodClient([
        StageError(ErrorCode.RUNPOD_TIMEOUT, "persistent outage", retryable=True)
    ])
    ctx = StageContext(
        job=await job_repo.get_job(upload_job.id),
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=fake,
        worker_id="poll-test",
    )

    with pytest.raises(StageError, match="persistent outage"):
        await handle_dispatched(ctx)
    assert fake.cancelled == ["rp-stale"]
    assert (await job_repo.get_job(upload_job.id)).worker_phase == "failed"


async def test_stale_worker_cancel_failure_does_not_mask_transient_poll_error(
    settings, job_repo, upload_job
):
    """wave3 #1: a non-retryable cancel failure during a stale-worker transient
    outage must not replace the original retryable poll error. The worker is
    still marked failed, and the transient error (not the cancel error)
    propagates so the job retries."""
    claimed = await job_repo.claim_next(worker_id="poll-test", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="poll-test",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="poll-test",
    )
    await job_repo.record_dispatch(
        upload_job.id, worker_id="poll-test", runpod_job_id="rp-stale"
    )
    settings.runpod_worker_stall_seconds = 10
    async with job_repo._sf() as session, session.begin():
        row = await session.get(Job, upload_job.id)
        row.worker_heartbeat_at = utcnow() - timedelta(seconds=11)

    fake = FakeRunPodClient(
        [StageError(ErrorCode.RUNPOD_TIMEOUT, "persistent outage", retryable=True)],
        cancel_error=StageError(
            ErrorCode.RUNPOD_DISPATCH_FAILED, "cancel api down", retryable=False
        ),
    )
    ctx = StageContext(
        job=await job_repo.get_job(upload_job.id),
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=fake,
        worker_id="poll-test",
    )

    # The transient poll error propagates, NOT the non-retryable cancel error.
    with pytest.raises(StageError, match="persistent outage"):
        await handle_dispatched(ctx)
    assert fake.cancelled == ["rp-stale"]
    assert (await job_repo.get_job(upload_job.id)).worker_phase == "failed"
    # The non-retryable cancel error never lands on the job.
    job = await job_repo.get_job(upload_job.id)
    assert job.error_code != "RUNPOD_DISPATCH_FAILED"


async def test_stale_worker_does_not_cancel_new_owners_runpod_job(
    settings, job_repo, upload_job
):
    """B2 remote-effect fence (PR 42 review): after a reclaim, the evicted
    worker hitting the queue-timeout branch must not cancel the new owner's
    RunPod job and must not fail the job — it yields. The owner's path still
    cancels."""
    claimed = await job_repo.claim_next(worker_id="A", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="A",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="A",
    )
    await job_repo.record_dispatch(
        upload_job.id, worker_id="A", runpod_job_id="rp-shared"
    )
    # Backdate the heartbeat so the queue timeout is already exceeded, then
    # let B reclaim A's expired lease.
    async with job_repo._sf() as session, session.begin():
        row = await session.get(Job, upload_job.id)
        row.worker_heartbeat_at = utcnow() - timedelta(seconds=60)
        row.lease_expires_at = utcnow() - timedelta(seconds=1)
    reclaimed = await job_repo.claim_next(worker_id="B", lease_seconds=60)
    assert reclaimed is not None and reclaimed.lease_owner == "B"

    settings.runpod_queue_timeout_seconds = 10
    stale_fake = FakeRunPodClient([{"status": "IN_QUEUE", "output": {}}])
    ctx = StageContext(
        job=await job_repo.get_job(upload_job.id),
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=stale_fake,
        worker_id="A",
    )
    assert await handle_dispatched(ctx) is None  # yields: no cancel, no StageError
    assert stale_fake.cancelled == []  # the new owner's RunPod job is untouched
    job = await job_repo.get_job(upload_job.id)
    assert job.status is JobStage.dispatched
    assert job.lease_owner == "B"
    assert job.worker_phase == "dispatched"  # A's failed-write was skipped too

    # The owner path still cancels and raises.
    owner_fake = FakeRunPodClient([{"status": "IN_QUEUE", "output": {}}])
    ctx_b = StageContext(
        job=await job_repo.get_job(upload_job.id),
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=owner_fake,
        worker_id="B",
    )
    with pytest.raises(StageError) as excinfo:
        await handle_dispatched(ctx_b)
    assert excinfo.value.code is ErrorCode.RUNPOD_TIMEOUT
    assert owner_fake.cancelled == ["rp-shared"]
    assert (await job_repo.get_job(upload_job.id)).worker_phase == "failed"


async def test_owner_with_failed_phase_still_cancels_on_timeout(
    settings, job_repo, upload_job
):
    """Batch-3 correction: the cancel gate must be lease ownership, not the
    record_worker_progress return (which is also False when the phase is
    merely unchanged). An owner whose row's worker_phase was concurrently
    set to 'failed' still cancels and raises RUNPOD_TIMEOUT."""
    claimed = await job_repo.claim_next(worker_id="B", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="B",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="B",
    )
    await job_repo.record_dispatch(
        upload_job.id, worker_id="B", runpod_job_id="rp-still-queued"
    )
    # Backdate the heartbeat past the queue timeout (visible in the handler's
    # snapshot), then flip the row's phase to 'failed' after the snapshot is
    # taken — the concurrent-write race where record_worker_progress would
    # return False for the rightful owner.
    settings.runpod_queue_timeout_seconds = 10
    async with job_repo._sf() as session, session.begin():
        row = await session.get(Job, upload_job.id)
        row.worker_heartbeat_at = utcnow() - timedelta(seconds=60)
    snapshot = await job_repo.get_job(upload_job.id)
    assert snapshot.worker_phase == "dispatched"
    async with job_repo._sf() as session, session.begin():
        row = await session.get(Job, upload_job.id)
        row.worker_phase = "failed"

    fake = FakeRunPodClient([{"status": "IN_QUEUE", "output": {}}])
    ctx = StageContext(
        job=snapshot,
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=fake,
        worker_id="B",
    )
    with pytest.raises(StageError) as excinfo:
        await handle_dispatched(ctx)
    assert excinfo.value.code is ErrorCode.RUNPOD_TIMEOUT
    assert fake.cancelled == ["rp-still-queued"]


async def test_transient_poll_failure_dedupes_one_event_per_outage(
    settings, job_repo, upload_job
):
    """wave3 #3: a continuous outage records at most one runpod_poll_failed
    event, regardless of how many poll iterations it spans."""
    claimed = await job_repo.claim_next(worker_id="poll-test", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="poll-test",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="poll-test",
    )
    await job_repo.record_dispatch(
        upload_job.id, worker_id="poll-test", runpod_job_id="rp-poll"
    )
    job = await job_repo.get_job(upload_job.id)
    settings.runpod_worker_stall_seconds = 10_000  # never trip the stale path
    fake = FakeRunPodClient([
        StageError(ErrorCode.RUNPOD_TIMEOUT, "temporary", retryable=True)
    ])

    async def _poll_once() -> None:
        ctx = StageContext(
            job=job,
            settings=settings,
            pipeline=None,  # type: ignore[arg-type]
            jobs=job_repo,
            runpod=fake,
            worker_id="poll-test",
        )
        assert await handle_dispatched(ctx) is None

    # Three poll iterations of the same outage.
    for _ in range(3):
        await _poll_once()

    failures = [
        event for event in await job_repo.list_events(upload_job.id)
        if event.event == "runpod_poll_failed"
    ]
    assert len(failures) == 1
    assert failures[0].detail["error_code"] == "RUNPOD_TIMEOUT"
    assert len(fake.polled) == 3  # the dedup is on events, not on polls


async def test_successful_poll_resets_outage_even_when_phase_is_unchanged(
    settings, job_repo, upload_job
):
    claimed = await job_repo.claim_next(worker_id="poll-test", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="poll-test",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="poll-test",
    )
    await job_repo.record_dispatch(
        upload_job.id, worker_id="poll-test", runpod_job_id="rp-poll"
    )
    await job_repo.record_worker_progress(
        upload_job.id, phase="working", worker_id="poll-test"
    )
    settings.runpod_worker_stall_seconds = 10_000
    fake = FakeRunPodClient([
        StageError(ErrorCode.RUNPOD_TIMEOUT, "first outage", retryable=True),
        {"status": "IN_PROGRESS", "output": {"phase": "working"}},
        StageError(ErrorCode.RUNPOD_TIMEOUT, "second outage", retryable=True),
    ])

    for _ in range(3):
        ctx = StageContext(
            job=await job_repo.get_job(upload_job.id),
            settings=settings,
            pipeline=None,  # type: ignore[arg-type]
            jobs=job_repo,
            runpod=fake,
            worker_id="poll-test",
        )
        assert await handle_dispatched(ctx) is None

    events = [event.event for event in await job_repo.list_events(upload_job.id)]
    assert events.count("runpod_poll_failed") == 2
    assert events.count("runpod_poll_recovered") == 1


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("handoff", ["present", "missing", "read_error"])
async def test_superseded_completion_requires_handoff(
    settings, job_repo, upload_job, monkeypatch, legacy, handoff
):
    from botocore.exceptions import ClientError

    await job_repo.claim_next(worker_id="superseded-test", lease_seconds=60)
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.pending, to_stage=JobStage.downloading,
        worker_id="superseded-test",
    )
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.downloading, to_stage=JobStage.dispatched,
        worker_id="superseded-test",
    )
    dispatch_key = f"{upload_job.id.hex}:0"
    if legacy:
        await job_repo.record_dispatch(
            upload_job.id, worker_id="superseded-test", runpod_job_id="rp-complete"
        )
    else:
        await job_repo.reserve_dispatch(
            upload_job.id, worker_id="superseded-test", idempotency_key=dispatch_key
        )
        await job_repo.confirm_dispatch(
            upload_job.id, idempotency_key=dispatch_key, runpod_job_id="rp-complete"
        )
    track_id = track_id_for_job(upload_job.id)
    prefix = (
        cloud.separation_prefix(track_id) if legacy
        else cloud.attempt_prefix(track_id, dispatch_key)
    )
    inspected = []

    def head_object(*, Bucket, Key):
        inspected.append((Bucket, Key))
        if handoff == "present":
            return {"ContentLength": 1}
        raise ClientError(
            {"Error": {"Code": "NoSuchKey" if handoff == "missing" else "AccessDenied"},
             "ResponseMetadata": {"HTTPStatusCode": 404 if handoff == "missing" else 403}},
            "HeadObject",
        )

    monkeypatch.setattr(cloud, "s3_client", lambda _settings: SimpleNamespace(
        head_object=head_object
    ))
    ctx = StageContext(
        job=await job_repo.get_job(upload_job.id), settings=settings,
        pipeline=None, jobs=job_repo, worker_id="superseded-test",
        runpod=FakeRunPodClient([{
            "status": "COMPLETED",
            "output": {"status": "SUPERSEDED", "package_prefix": "wrong/attempt"},
        }]),
    )
    if handoff == "present":
        assert await handle_dispatched(ctx) == JobStage.verifying
        if not legacy:
            assert ctx.detail["package_prefix"] == prefix
    else:
        with pytest.raises(StageError) as excinfo:
            await handle_dispatched(ctx)
        assert excinfo.value.retryable is True
        expected_code = (
            ErrorCode.RUNPOD_DISPATCH_FAILED if handoff == "missing"
            else ErrorCode.S3_UPLOAD_FAILED
        )
        assert excinfo.value.code == expected_code
        assert "package_prefix" not in ctx.detail
    assert inspected == [(settings.s3_media_bucket, f"{prefix}/handoff.json")]
    job = await job_repo.get_job(upload_job.id)
    assert job.worker_phase == ("failed" if handoff == "missing" else "dispatched")
    events = await job_repo.list_events(upload_job.id)
    completed = [event for event in events if event.event == "worker_completed"]
    assert len(completed) == (1 if handoff == "present" else 0)


async def test_worker_completed_wraps_non_object_output(
    settings, job_repo, upload_job
):
    claimed = await job_repo.claim_next(worker_id="complete-test", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="complete-test",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="complete-test",
    )
    await job_repo.record_dispatch(
        upload_job.id, worker_id="complete-test", runpod_job_id="rp-complete"
    )
    ctx = StageContext(
        job=await job_repo.get_job(upload_job.id),
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=FakeRunPodClient([{"status": "COMPLETED", "output": "done"}]),
        worker_id="complete-test",
    )

    assert await handle_dispatched(ctx) == JobStage.verifying
    assert await handle_dispatched(ctx) == JobStage.verifying
    completed = [
        event for event in await job_repo.list_events(upload_job.id)
        if event.event == "worker_completed"
    ]
    assert len(completed) == 1
    assert completed[0].detail == {"output": "done"}


async def test_dispatch_timeout_reservation_prevents_redispatch(
    settings, job_repo, upload_job, monkeypatch
):
    class TimeoutOnce(FakeRunPodClient):
        async def dispatch(self, *, job_id, idempotency_key, payload):
            rid = f"rp-{len(self.dispatched) + 1}"
            self.dispatched.append((rid, idempotency_key, payload))
            if len(self.dispatched) == 1:
                raise StageError(ErrorCode.RUNPOD_TIMEOUT, "lost response", retryable=True)
            return rid

    job = await job_repo.claim_next(worker_id="dispatch-timeout", lease_seconds=60)
    assert job is not None and job.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="dispatch-timeout",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="dispatch-timeout",
    )

    async def package_not_ready(_ctx, *, idempotency_key):
        assert idempotency_key == f"{upload_job.id.hex}:0"
        return False

    async def dispatch_not_started(_ctx, *, idempotency_key):
        assert idempotency_key == f"{upload_job.id.hex}:0"
        return None

    monkeypatch.setattr(cloud, "package_ready", package_not_ready)
    monkeypatch.setattr(cloud, "accepted_dispatch_id", dispatch_not_started)
    fake = TimeoutOnce([{"status": "COMPLETED", "output": {}}])
    first = StageContext(
        job=job,
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=fake,
        worker_id="dispatch-timeout",
    )
    with pytest.raises(StageError, match="lost response"):
        await handle_dispatched(first)

    await job_repo.schedule_retry(
        upload_job.id,
        worker_id="dispatch-timeout",
        error_code=ErrorCode.RUNPOD_TIMEOUT,
        error_detail="lost response",
        retry_in_seconds=0,
    )
    job = await job_repo.claim_next(worker_id="dispatch-timeout", lease_seconds=60)
    reconcile = StageContext(
        job=job,
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=fake,
        worker_id="dispatch-timeout",
    )
    assert await handle_dispatched(reconcile) is None
    assert reconcile.park_seconds == settings.runpod_poll_seconds
    assert len(fake.dispatched) == 1

    await job_repo.park(
        upload_job.id, worker_id="dispatch-timeout", recheck_in_seconds=0
    )
    job = await job_repo.claim_next(worker_id="dispatch-timeout", lease_seconds=60)
    reconcile_again = StageContext(
        job=job,
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=fake,
        worker_id="dispatch-timeout",
    )
    assert await handle_dispatched(reconcile_again) is None
    assert len(fake.dispatched) == 1
    events = [event.event for event in await job_repo.list_events(upload_job.id)]
    assert events.count("runpod_dispatch_started") == 1
    assert "runpod_dispatched" not in events

    fail_closed_after = (
        settings.runpod_queue_timeout_seconds
        + settings.runpod_worker_stall_seconds
        + 1
    )
    monkeypatch.setattr(
        "shizzle_server.orchestrator.stages._age_seconds",
        lambda _since: fail_closed_after,
    )
    await job_repo.park(
        upload_job.id, worker_id="dispatch-timeout", recheck_in_seconds=0
    )
    job = await job_repo.claim_next(worker_id="dispatch-timeout", lease_seconds=60)
    aged = StageContext(
        job=job,
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=fake,
        worker_id="dispatch-timeout",
    )
    with pytest.raises(StageError) as exc:
        await handle_dispatched(aged)
    assert exc.value.code == ErrorCode.RUNPOD_TIMEOUT
    assert exc.value.retryable is False
    assert len(fake.dispatched) == 1
    events = [event.event for event in await job_repo.list_events(upload_job.id)]
    assert events.count("runpod_dispatch_reconciliation_failed") == 1


async def test_legacy_unconfirmed_package_uses_base_prefix(
    settings, job_repo, upload_job, monkeypatch
):
    job = await job_repo.claim_next(worker_id="legacy-recovery", lease_seconds=60)
    assert job is not None and job.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="legacy-recovery",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="legacy-recovery",
    )
    await job_repo.append_event(
        upload_job.id, "dispatch_unconfirmed", {"attempt": 0}
    )

    async def no_receipt(_ctx, *, idempotency_key):
        assert idempotency_key is None
        return None

    async def legacy_package_ready(_ctx, *, idempotency_key):
        assert idempotency_key is None
        return True

    monkeypatch.setattr(cloud, "accepted_dispatch_id", no_receipt)
    monkeypatch.setattr(cloud, "package_ready", legacy_package_ready)
    ctx = StageContext(
        job=job,
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=FakeRunPodClient([]),
        worker_id="legacy-recovery",
    )

    assert await handle_dispatched(ctx) == JobStage.verifying
    assert ctx.detail["package_prefix"] == cloud.separation_prefix(
        track_id_for_job(upload_job.id)
    )


async def test_accepted_dispatch_survives_confirmation_failure(
    settings, job_repo, upload_job, monkeypatch
):
    job = await job_repo.claim_next(worker_id="confirm-failure", lease_seconds=60)
    assert job is not None and job.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="confirm-failure",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="confirm-failure",
    )
    fake = FakeRunPodClient([{"status": "COMPLETED", "output": {}}])
    original_confirm = job_repo.confirm_dispatch
    confirmation_attempts = 0

    async def fail_confirmation(*_args, **_kwargs):
        nonlocal confirmation_attempts
        confirmation_attempts += 1
        raise RuntimeError("database unavailable after RunPod accepted dispatch")

    monkeypatch.setattr(job_repo, "confirm_dispatch", fail_confirmation)
    first = StageContext(
        job=job,
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=fake,
        worker_id="confirm-failure",
    )
    with pytest.raises(RuntimeError, match="database unavailable"):
        await handle_dispatched(first)

    assert confirmation_attempts == 3
    assert len(fake.dispatched) == 1
    monkeypatch.setattr(job_repo, "confirm_dispatch", original_confirm)
    await job_repo.schedule_retry(
        upload_job.id,
        worker_id="confirm-failure",
        error_code=ErrorCode.INTERNAL,
        error_detail="database unavailable",
        retry_in_seconds=0,
    )
    job = await job_repo.claim_next(worker_id="confirm-failure", lease_seconds=60)

    async def package_ready(_ctx, *, idempotency_key):
        assert idempotency_key == f"{upload_job.id.hex}:0"
        return True

    async def no_dispatch_receipt(_ctx, *, idempotency_key):
        assert idempotency_key == f"{upload_job.id.hex}:0"
        return None

    monkeypatch.setattr(cloud, "package_ready", package_ready)
    monkeypatch.setattr(cloud, "accepted_dispatch_id", no_dispatch_receipt)
    recovered = StageContext(
        job=job,
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=fake,
        worker_id="confirm-failure",
    )
    assert await handle_dispatched(recovered) == JobStage.verifying
    assert recovered.detail["package_prefix"] == cloud.attempt_prefix(
        track_id_for_job(upload_job.id), f"{upload_job.id.hex}:0"
    )
    assert len(fake.dispatched) == 1
    events = await job_repo.list_events(upload_job.id)
    assert [event.event for event in events].count("runpod_dispatch_started") == 1
    recovery = [event for event in events if event.event == "runpod_dispatch_recovered"]
    assert len(recovery) == 1
    assert recovery[0].detail == {
        "idempotency_key": f"{upload_job.id.hex}:0",
        "source": "handoff.json",
    }


async def test_accepted_dispatch_recovers_remote_id_from_worker_receipt(
    settings, job_repo, upload_job, monkeypatch
):
    job = await job_repo.claim_next(worker_id="receipt-recovery", lease_seconds=60)
    assert job is not None and job.id == upload_job.id
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        worker_id="receipt-recovery",
    )
    await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        worker_id="receipt-recovery",
    )
    dispatch_key = f"{upload_job.id.hex}:0"
    assert await job_repo.reserve_dispatch(
        upload_job.id,
        worker_id="receipt-recovery",
        idempotency_key=dispatch_key,
    )

    async def package_not_ready(_ctx, *, idempotency_key):
        assert idempotency_key == dispatch_key
        return False

    async def accepted_dispatch(_ctx, *, idempotency_key):
        assert idempotency_key == dispatch_key
        return "rp-accepted"

    monkeypatch.setattr(cloud, "package_ready", package_not_ready)
    monkeypatch.setattr(cloud, "accepted_dispatch_id", accepted_dispatch)
    ctx = StageContext(
        job=job,
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=FakeRunPodClient([{"status": "IN_QUEUE"}]),
        worker_id="receipt-recovery",
    )

    assert await handle_dispatched(ctx) is None
    assert ctx.park_seconds == settings.runpod_poll_seconds
    recovered = await job_repo.get_job(upload_job.id)
    assert recovered.runpod_job_id == "rp-accepted"
    events = await job_repo.list_events(upload_job.id)
    assert [event.event for event in events].count("runpod_dispatched") == 1
    receipt_events = [
        event for event in events if event.event == "runpod_dispatch_recovered"
    ]
    assert len(receipt_events) == 1
    assert receipt_events[0].detail == {
        "idempotency_key": dispatch_key,
        "runpod_job_id": "rp-accepted",
        "source": "dispatch.json",
    }


async def test_cloud_verifying_missing_handoff_is_retryable(
    settings, monkeypatch
):
    ctx = _ctx_for_upload(settings, settings.data_dir)
    monkeypatch.setattr(cloud, "s3_client", lambda _settings: object())

    def missing(*_args):
        from shizzle_server.publish.lossless_intake import PackageNotReady

        raise PackageNotReady("package has not crossed the interface")

    monkeypatch.setattr(cloud, "download_package", missing)
    with pytest.raises(StageError) as exc:
        await cloud.cloud_verifying(ctx)
    assert exc.value.code is ErrorCode.CHECKSUM_MISMATCH
    assert exc.value.retryable is True


async def test_cloud_verifying_rejects_source_hash_mismatch(
    settings, monkeypatch
):
    ctx = _ctx_for_upload(settings, settings.data_dir)
    ctx.job.input_checksum = "expected"
    monkeypatch.setattr(cloud, "s3_client", lambda _settings: object())
    monkeypatch.setattr(cloud, "download_package", lambda *_args: None)
    monkeypatch.setattr(
        cloud,
        "load_and_verify_package",
        lambda _path: SimpleNamespace(
            handoff={
                "source": {"sha256": "wrong"},
                "separation": {"sample_count": 44100},
            },
            duration_seconds=1.0,
        ),
    )

    with pytest.raises(StageError) as exc:
        await cloud.cloud_verifying(ctx)
    assert exc.value.code is ErrorCode.CHECKSUM_MISMATCH
    assert exc.value.retryable is False


async def test_cloud_verifying_rejects_source_object_mismatch(
    settings, monkeypatch
):
    ctx = _ctx_for_upload(settings, settings.data_dir)
    ctx.job.input_checksum = "expected"
    monkeypatch.setattr(cloud, "s3_client", lambda _settings: object())
    monkeypatch.setattr(cloud, "download_package", lambda *_args: None)
    monkeypatch.setattr(
        cloud,
        "load_and_verify_package",
        lambda _path: SimpleNamespace(
            handoff={
                "source": {
                    "sha256": "expected",
                    "object_key": "sources/different/source.mp4",
                },
                "separation": {"sample_count": 44100},
            },
            duration_seconds=1.0,
        ),
    )

    with pytest.raises(StageError) as exc:
        await cloud.cloud_verifying(ctx)
    assert exc.value.code is ErrorCode.CHECKSUM_MISMATCH
    assert exc.value.retryable is False


async def test_cloud_publish_cleans_job_dir_and_persists_verification(
    settings, job_repo, track_repo, upload_job, monkeypatch
):
    claimed = await job_repo.claim_next(worker_id="publish-test", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    for before, after in [
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.dispatched),
        (JobStage.dispatched, JobStage.verifying),
        (JobStage.verifying, JobStage.publishing),
    ]:
        job = await job_repo.advance(
            upload_job.id, from_stage=before, to_stage=after, worker_id="publish-test"
        )

    class Verification:
        def to_integrity(self):
            return {"policy": "auto", "object_count": 8}

    class FakePublisher:
        def __init__(self, *_args):
            pass

        async def publish_async(self, *_args):
            tid = track_id_for_job(upload_job.id)
            return SimpleNamespace(
                s3_prefix=f"tracks/{tid}/1",
                manifest_key=f"tracks/{tid}/1/manifest.json",
                verification=Verification(),
            )

    monkeypatch.setattr(cloud, "s3_client", lambda _settings: object())
    monkeypatch.setattr(
        cloud, "load_and_verify_package", lambda _path: SimpleNamespace()
    )
    monkeypatch.setattr(
        cloud,
        "transform",
        lambda *_args: {
            "title": "Cloud",
            "artist": "",
            "duration": 1.0,
            "integrity": {"worker_image": "worker:sha"},
        },
    )
    monkeypatch.setattr(cloud, "stage", lambda *_args: [])
    monkeypatch.setattr(cloud, "Publisher", FakePublisher)
    ctx = StageContext(
        job=job,
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=None,  # type: ignore[arg-type]
        worker_id="publish-test",
    )

    assert await cloud.cloud_publishing(ctx) == JobStage.ready
    assert not ctx.job_dir.exists()
    track = await track_repo.get(track_id_for_job(upload_job.id))
    assert track.integrity == {
        "worker_image": "worker:sha",
        "publisher": {"policy": "auto", "object_count": 8},
    }


async def test_cloud_publish_plumbs_job_artist_to_manifest_and_track(
    settings, job_repo, track_repo, monkeypatch
):
    """The manifest's artist comes from the job, and the track falls back to
    the job's artist when the manifest carries none."""
    from shizzle_server.db.models import SourceType

    job_id = uuid.uuid4()
    job_dir = settings.data_dir / job_id.hex
    job_dir.mkdir(parents=True)
    (job_dir / "source.mp4").write_bytes(b"fake video bytes")
    job = await job_repo.create_job(
        job_id=job_id,
        source_type=SourceType.upload,
        source_ref="source.mp4",
        title="Spoonman",
        artist="Soundgarden",
    )
    claimed = await job_repo.claim_next(worker_id="publish-test", lease_seconds=60)
    assert claimed is not None and claimed.id == job.id
    for before, after in [
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.dispatched),
        (JobStage.dispatched, JobStage.verifying),
        (JobStage.verifying, JobStage.publishing),
    ]:
        job = await job_repo.advance(
            job.id, from_stage=before, to_stage=after, worker_id="publish-test"
        )

    class Verification:
        def to_integrity(self):
            return {"policy": "auto", "object_count": 8}

    class FakePublisher:
        def __init__(self, *_args):
            pass

        async def publish_async(self, *_args):
            tid = track_id_for_job(job.id)
            return SimpleNamespace(
                s3_prefix=f"tracks/{tid}/1",
                manifest_key=f"tracks/{tid}/1/manifest.json",
                verification=Verification(),
            )

    transform_calls = []

    def _transform(_pkg, _source, _candidate, title, artist):
        transform_calls.append((title, artist))
        return {"title": "Cloud", "duration": 1.0, "integrity": {}}

    monkeypatch.setattr(cloud, "s3_client", lambda _settings: object())
    monkeypatch.setattr(
        cloud, "load_and_verify_package", lambda _path: SimpleNamespace()
    )
    monkeypatch.setattr(cloud, "transform", _transform)
    monkeypatch.setattr(cloud, "stage", lambda *_args: [])
    monkeypatch.setattr(cloud, "Publisher", FakePublisher)
    ctx = StageContext(
        job=job,
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=None,  # type: ignore[arg-type]
        worker_id="publish-test",
    )

    assert await cloud.cloud_publishing(ctx) == JobStage.ready
    assert transform_calls == [("Spoonman", "Soundgarden")]
    track = await track_repo.get(track_id_for_job(job.id))
    assert track.artist == "Soundgarden"


async def test_cloud_publish_failures_retain_job_dir_and_map_retryability(
    settings, job_repo, upload_job, monkeypatch
):
    from shizzle_server.publish.lossless_intake import IntakeError

    claimed = await job_repo.claim_next(worker_id="publish-test", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    for before, after in [
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.dispatched),
        (JobStage.dispatched, JobStage.verifying),
        (JobStage.verifying, JobStage.publishing),
    ]:
        job = await job_repo.advance(
            upload_job.id, from_stage=before, to_stage=after, worker_id="publish-test"
        )
    monkeypatch.setattr(cloud, "s3_client", lambda _settings: object())
    monkeypatch.setattr(
        cloud, "load_and_verify_package", lambda _path: SimpleNamespace()
    )
    monkeypatch.setattr(
        cloud,
        "transform",
        lambda *_args: (_ for _ in ()).throw(IntakeError("ffmpeg gate")),
    )
    ctx = StageContext(
        job=job,
        settings=settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=job_repo,
        runpod=None,  # type: ignore[arg-type]
        worker_id="publish-test",
    )

    with pytest.raises(StageError) as intake:
        await cloud.cloud_publishing(ctx)
    assert intake.value.code is ErrorCode.PUBLISH_FAILED
    assert intake.value.retryable is False
    assert ctx.job_dir.exists()

    monkeypatch.setattr(
        cloud,
        "transform",
        lambda *_args: {"title": "Cloud", "duration": 1.0, "integrity": {}},
    )
    monkeypatch.setattr(
        cloud, "stage", lambda *_args: (_ for _ in ()).throw(RuntimeError("S3 down"))
    )
    with pytest.raises(StageError) as transient:
        await cloud.cloud_publishing(ctx)
    assert transient.value.code is ErrorCode.PUBLISH_FAILED
    assert transient.value.retryable is True
    assert ctx.job_dir.exists()


async def _poll_predicate(pred, interval: float = 0.05):
    """Tiny helper: poll an async predicate until truthy (no timeout here)."""
    while True:
        value = await pred()
        if value:
            return value
        await asyncio.sleep(interval)

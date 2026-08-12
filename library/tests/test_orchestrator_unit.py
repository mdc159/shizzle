"""Unit: orchestrator loop mechanics on SQLite (claims, leases, retries,
duplicate completion). Real-Postgres concurrency lives in tests/contract/."""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shizzle_server.api.media import _s3_client
from shizzle_server.db.models import Job, JobStage, SourceType, utcnow
from shizzle_server.db.repository import track_id_for_job
from shizzle_server.orchestrator import cloud
from shizzle_server.orchestrator.loop import Orchestrator
from shizzle_server.orchestrator.stages import StageContext
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


async def test_dispatched_stage_is_claimed_by_loop(job_repo):
    """``dispatched`` is owned by the claim-based RunPod reconciler (WS4): the
    lease loop claims it and handle_dispatched parks between polls."""
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://x")
    await job_repo.advance(job.id, from_stage=JobStage.pending, to_stage=JobStage.downloading)
    await job_repo.advance(job.id, from_stage=JobStage.downloading, to_stage=JobStage.dispatched)
    claimed = await job_repo.claim_next(worker_id="A", lease_seconds=120)
    assert claimed is not None and claimed.id == job.id and claimed.status == JobStage.dispatched


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


# --- WS4: dispatched reconciliation (FakeRunPodClient) ------------------------
# These exercise handle_dispatched through the real loop: park-on-None, phase
# recording on change, queue/stall watchdogs, and fresh re-dispatch after a
# terminal failure. cloud_verifying/publishing are stubbed (ffmpeg lives in the
# WS5 integration gate); upload_source is stubbed so no S3 is needed.


class FakeRunPodClient:
    """Scripted RunPod client. ``poll`` returns scripted responses in order,
    repeating the last one when the script is exhausted (keeps stall/queue
    watchdog tests deterministic)."""

    def __init__(self, responses: list[dict]) -> None:
        self.responses = list(responses)
        self._i = 0
        self.dispatched: list[tuple[str, str, dict]] = []
        self.polled: list[str] = []
        self.cancelled: list[str] = []

    async def dispatch(self, *, job_id, idempotency_key, payload):
        rid = f"rp-{len(self.dispatched) + 1}"
        self.dispatched.append((rid, idempotency_key, payload))
        return rid

    async def poll(self, runpod_job_id: str) -> dict:
        self.polled.append(runpod_job_id)
        resp = self.responses[min(self._i, len(self.responses) - 1)]
        self._i += 1
        return resp

    async def cancel(self, runpod_job_id: str) -> None:
        self.cancelled.append(runpod_job_id)


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
            ctx.job.id, title=ctx.job.title or ctx.job.id.hex, artist="",
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
    orch = Orchestrator(settings, worker_id="cloud-unit")
    orch.runpod = fake
    task = asyncio.create_task(orch.run_forever())
    return orch, task


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


async def test_cloud_dispatched_stall_cancels_and_schedules_retry(
    settings, job_repo, upload_job, monkeypatch
):
    """Frozen heartbeat (phase never changes) trips the stall watchdog:
    cancel is called and a retry is scheduled (attempt increments)."""
    _stub_cloud_intake(monkeypatch)
    settings.runpod_poll_seconds = 0.02
    settings.runpod_worker_stall_seconds = 0.05
    fake = FakeRunPodClient([{"status": "IN_PROGRESS", "output": {"phase": "working"}}])
    orch, task = await _run_cloud_orch(settings, fake)
    try:
        # Wait for the first retry (attempt 1) scheduled by the stall error.
        async def _retry_scheduled():
            events = await job_repo.list_events(upload_job.id)
            return any(e.event == "retry_scheduled" for e in events) or None

        await asyncio.wait_for(_poll_predicate(_retry_scheduled), timeout=15)
    finally:
        orch.request_stop()
        await asyncio.wait_for(task, timeout=10)

    assert len(fake.cancelled) >= 1
    job = await job_repo.get_job(upload_job.id)
    assert job.attempt >= 1
    assert job.error_code == "RUNPOD_TIMEOUT"


async def test_cloud_dispatched_runpod_failed_redispatches_fresh(
    settings, job_repo, upload_job, monkeypatch
):
    """A terminal RunPod failure marks the runpod id dead; the retry dispatches
    a fresh job under a new idempotency key and still reaches ready."""
    _stub_cloud_intake(monkeypatch)
    settings.runpod_poll_seconds = 0.02
    fake = FakeRunPodClient([
        {"status": "FAILED", "output": {"error": "boom"}},
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
        await asyncio.sleep(0.4)  # several park/recheck cycles
    finally:
        orch.request_stop()
        await asyncio.wait_for(task, timeout=10)

    job = await job_repo.get_job(upload_job.id)
    assert job.status == JobStage.dispatched
    assert job.attempt == 0
    assert job.runpod_job_id is not None
    assert len(fake.dispatched) == 1  # parked, never re-dispatched


async def _poll_predicate(pred, interval: float = 0.05):
    """Tiny helper: poll an async predicate until truthy (no timeout here)."""
    while True:
        value = await pred()
        if value:
            return value
        await asyncio.sleep(interval)

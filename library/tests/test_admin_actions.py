"""Administrator failed-job and library actions (G7-G9), fully offline."""

from __future__ import annotations

import hashlib
import json
import struct
import uuid
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from shizzle_server.api import admin as admin_module
from shizzle_server.api.media import _s3_client
from shizzle_server.db.models import (
    ALLOWED_TRANSITIONS,
    Job,
    JobEvent,
    JobStage,
    NodeAssignmentKind,
    NodeAssignmentStatus,
    SourceType,
    Track,
    utcnow,
)
from shizzle_server.db.repository import (
    InvalidTransition,
    pending_runpod_dispatch,
    track_id_for_job,
)
from shizzle_server.errors import ErrorCode, StageError
from shizzle_server.main import create_app
from shizzle_server.orchestrator import cloud

SECRET = "admin-action-test-secret"
HEADERS = {"x-shizzle-admin": SECRET}


@pytest_asyncio.fixture
async def admin_client(settings):
    settings.shizzle_admin_proxy_secret = SECRET
    app = create_app(settings)
    async with app.router.lifespan_context(app), AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client, app


@pytest.fixture(autouse=True)
def offline_s3(monkeypatch):
    _s3_client.cache_clear()
    monkeypatch.setattr(
        admin_module,
        "_head_blocking",
        lambda _settings, _key: admin_module._HeadOutcome(None, None),
    )

    async def empty_attempt(_ctx, *, idempotency_key):
        assert isinstance(idempotency_key, str)
        return cloud.DispatchAttemptInspection(
            receipt_present=False, runpod_job_id=None, handoff_present=False
        )

    monkeypatch.setattr(cloud, "inspect_dispatch_attempt", empty_attempt)
    yield
    _s3_client.cache_clear()


async def _job(app, source_type=SourceType.upload, **kwargs):
    return await app.state.job_repo.create_job(
        source_type=source_type,
        source_ref=kwargs.pop("source_ref", "source.mp4"),
        **kwargs,
    )


async def _claim_and_advance(app, job, to_stage):
    worker = "action-test"
    claimed = await app.state.job_repo.claim_next(worker_id=worker, lease_seconds=600)
    assert claimed is not None and claimed.id == job.id
    if to_stage == JobStage.pending:
        return claimed
    stages = (
        JobStage.pending,
        JobStage.downloading,
        JobStage.dispatched,
        JobStage.verifying,
        JobStage.publishing,
    )
    for from_stage, next_stage in zip(stages, stages[1:], strict=False):
        if from_stage == JobStage.dispatched:
            key = f"{job.id.hex}:0"
            assert await app.state.job_repo.reserve_dispatch(
                job.id, worker_id=worker, idempotency_key=key
            )
            await app.state.job_repo.confirm_dispatch(
                job.id, idempotency_key=key, runpod_job_id="rp-complete"
            )
        detail = None
        if from_stage == JobStage.dispatched:
            detail = {"package_prefix": cloud.attempt_prefix(track_id_for_job(job.id), key)}
        await app.state.job_repo.advance(
            job.id,
            worker_id=worker,
            from_stage=from_stage,
            to_stage=next_stage,
            detail=detail,
        )
        if next_stage == to_stage:
            return claimed
    raise AssertionError("unsupported target stage")


async def _fail(app, job, stage, code=ErrorCode.RUNPOD_DISPATCH_FAILED):
    claimed = await _claim_and_advance(app, job, stage)
    await app.state.job_repo.fail_job(
        job.id,
        worker_id="action-test",
        error_code=code,
        error_detail="action test failure",
    )
    return claimed


async def _fail_with_pending(
    app,
    *,
    code=ErrorCode.RUNPOD_TIMEOUT,
    detail="submit may have been accepted",
    provider_accepted=None,
    input_checksum=None,
):
    job = await _job(app, input_checksum=input_checksum)
    _source(app, job)
    await _claim_and_advance(app, job, JobStage.dispatched)
    key = f"{job.id.hex}:0"
    assert await app.state.job_repo.reserve_dispatch(
        job.id, worker_id="action-test", idempotency_key=key
    )
    await app.state.job_repo.fail_job(
        job.id,
        worker_id="action-test",
        error_code=code,
        error_detail=detail,
        provider_accepted=provider_accepted,
    )
    pending = next(
        event
        for event in await app.state.job_repo.list_events(job.id)
        if event.event == "runpod_dispatch_started"
    )
    return job, key, pending


async def _age_reservation(session_factory, pending, settings, extra=1.0):
    window = admin_module.dispatch_reconciliation_window(settings)
    async with session_factory() as session, session.begin():
        event = await session.get(JobEvent, pending.id)
        event.created_at = utcnow() - timedelta(seconds=window + extra)


def _write_real_lossless_package(root, job, checksum):
    roles = ("vocals", "drums", "bass", "guitar", "piano", "shizzle")
    root.mkdir(parents=True, exist_ok=True)
    stems = []
    for role in roles:
        path = root / "stems" / f"{role}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        sample_count = 4410
        samples = [value for index in range(sample_count) for value in (0.0, 0.1 if index % 2 else -0.1)]
        frames = struct.pack(f"<{len(samples)}f", *samples)
        format_payload = struct.pack(
            "<HHIIHH",
            3,  # WAVE_FORMAT_IEEE_FLOAT
            2,
            44100,
            44100 * 2 * 4,
            2 * 4,
            32,
        )
        payload = (
            b"WAVEfmt " + struct.pack("<I", len(format_payload)) + format_payload
            + b"data" + struct.pack("<I", len(frames)) + frames
        )
        path.write_bytes(b"RIFF" + struct.pack("<I", len(payload)) + payload)
        stems.append({
            "role": role,
            "file": f"stems/{role}.wav",
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    handoff = {
        "interface": "lossless-stem-v1",
        "source": {
            "object_key": cloud.source_key(track_id_for_job(job.id)),
            "sha256": checksum,
        },
        "separation": {
            "model": "htdemucs_6s",
            "model_version": "test",
            "worker_image": "worker:test",
            "sample_rate_hz": 44100,
            "channels": 2,
            "sample_format": "f32le",
            "start_sample": 0,
            "sample_count": 4410,
        },
        "stems": stems,
    }
    (root / "handoff.json").write_text(json.dumps(handoff))


def _source(app, job):
    directory = app.state.settings.data_dir / job.id.hex
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "source.mp4").write_bytes(b"source")
    return directory


async def test_retry_restarts_with_fresh_attempt_and_expires_open_assignments(
    admin_client, monkeypatch
):
    c, app = admin_client
    await app.state.node_repo.heartbeat("mac-mini", capabilities={"separate": True})
    job = await _job(app)
    _source(app, job)
    claimed = await app.state.job_repo.claim_next(worker_id="action-test", lease_seconds=600)
    assignment = await app.state.node_repo.create_assignment(
        job.id,
        worker_id="action-test",
        node_id="mac-mini",
        kind=NodeAssignmentKind.separate,
        payload={},
    )
    assert claimed is not None and assignment is not None
    await app.state.job_repo.advance(
        job.id, worker_id="action-test", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await app.state.job_repo.advance(
        job.id, worker_id="action-test", from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    key = f"{job.id.hex}:0"
    assert await app.state.job_repo.reserve_dispatch(
        job.id, worker_id="action-test", idempotency_key=key
    )
    await app.state.job_repo.confirm_dispatch(
        job.id, idempotency_key=key, runpod_job_id="rp-live"
    )
    await app.state.job_repo.fail_job(
        job.id,
        worker_id="action-test",
        error_code=ErrorCode.RUNPOD_DISPATCH_FAILED,
        error_detail="failed",
    )

    cancelled = []

    class FakeRunPod:
        async def cancel(self, provider_id):
            cancelled.append(provider_id)

    monkeypatch.setattr(admin_module, "_runpod_for", lambda _settings: FakeRunPod())
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "status": "pending",
        "resumeStage": "pending",
        "attempt": 1,
        "action": "retried",
        "newJobId": None,
    }
    assert cancelled == ["rp-live"]
    current = await app.state.job_repo.get_job(job.id)
    assert current.status == JobStage.pending and current.attempt == 1
    assert current.runpod_job_id is None and current.error_code is None
    assert (await app.state.node_repo.get_assignment(assignment.id)).status == NodeAssignmentStatus.expired
    assert (await app.state.node_repo.get("mac-mini")).busy_assignment_id is None
    event = next(e for e in await app.state.job_repo.list_events(job.id) if e.event == "admin_retry_started")
    assert event.detail["previous_failure_code"] == "RUNPOD_DISPATCH_FAILED"
    assert event.detail["dispatch_key"] == f"{job.id.hex}:1" != key
    assert event.detail["runpod"]["status"] == "cancelled"


async def test_retry_refuses_missing_upload_source_but_uses_s3_fallback(
    admin_client, monkeypatch
):
    c, app = admin_client
    missing = await _job(app)
    await _fail(app, missing, JobStage.pending)
    refused = await c.post(f"/api/admin/jobs/{missing.id}/retry", headers=HEADERS)
    assert refused.status_code == 409
    assert "source no longer exists" in refused.json()["detail"]

    checksum = hashlib.sha256(b"source").hexdigest()
    present = await _job(app, input_checksum=checksum)
    await _fail(app, present, JobStage.pending)
    monkeypatch.setattr(
        admin_module,
        "_head_blocking",
        lambda _settings, _key: admin_module._HeadOutcome({"ContentLength": 6}, None),
    )

    class FakeS3:
        def download_file(self, _bucket, _key, destination):
            from pathlib import Path

            Path(destination).write_bytes(b"source")

    monkeypatch.setattr(admin_module, "s3_client", lambda _settings: FakeS3())
    accepted = await c.post(f"/api/admin/jobs/{present.id}/retry", headers=HEADERS)
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["resumeStage"] == "pending"

    # Acceptance is not enough: the restored bytes must let the real
    # downloading handler proceed instead of failing SOURCE_MISSING.
    from shizzle_server.orchestrator.stages import handle_downloading

    claimed = await app.state.job_repo.claim_next(worker_id="s3-retry", lease_seconds=60)
    assert claimed is not None and claimed.id == present.id
    local = app.state.settings.data_dir / present.id.hex / "source.mp4"
    assert local.read_bytes() == b"source"

    async def upload(_ctx):
        return cloud.source_key(track_id_for_job(present.id))

    monkeypatch.setattr(cloud, "upload_source", upload)
    app.state.settings.shizzle_pipeline = "cloud"
    ctx = SimpleNamespace(
        job=claimed,
        settings=app.state.settings,
        jobs=app.state.job_repo,
        worker_id="s3-retry",
        source_path=local,
        detail={},
    )
    assert await handle_downloading(ctx) == JobStage.dispatched
    await app.state.job_repo.advance(
        present.id,
        worker_id="s3-retry",
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
    )
    claimed = await app.state.job_repo.advance(
        present.id,
        worker_id="s3-retry",
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
    )
    ctx.job = claimed
    ctx.job_dir = local.parent

    def download_package(*_args):
        return None

    monkeypatch.setattr(cloud, "download_package", download_package)
    _write_real_lossless_package(local.parent / "package", present, checksum)
    assert await cloud.cloud_verifying(ctx) == JobStage.publishing


@pytest.mark.parametrize(
    ("stage", "code"),
    [
        (JobStage.verifying, ErrorCode.CHECKSUM_MISMATCH),
        (JobStage.publishing, ErrorCode.PUBLISH_FAILED),
    ],
)
async def test_publication_retry_reuses_selected_package(admin_client, monkeypatch, stage, code):
    c, app = admin_client
    checksum = hashlib.sha256(b"source").hexdigest()
    job = await _job(app, input_checksum=checksum)
    _source(app, job)
    await _fail(app, job, stage, code)

    async def complete(_ctx, *, allow_receipt):
        assert allow_receipt is True
        return "unit-completion-evidence"

    monkeypatch.setattr(admin_module, "completion_evidence", complete)
    monkeypatch.setattr(
        "shizzle_server.orchestrator.recover_publication.load_and_verify_package",
        lambda _path: SimpleNamespace(handoff={"source": {
            "sha256": checksum,
            "object_key": cloud.source_key(track_id_for_job(job.id)),
        }}),
    )
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "verifying"
    assert response.json()["resumeStage"] == "verifying"
    assert response.json()["attempt"] == 0
    current = await app.state.job_repo.get_job(job.id)
    assert current.status == JobStage.verifying
    assert current.runpod_job_id == "rp-complete"
    assert current.lease_owner is None
    events = await app.state.job_repo.list_events(job.id)
    assert "publication_recovery_started" in [event.event for event in events]
    retry = next(event for event in events if event.event == "admin_retry_started")
    assert retry.detail["resume_stage"] == "verifying"
    assert retry.detail["dispatch_key"] == f"{job.id.hex}:0"


async def test_url_publication_recovery_stays_in_place(admin_client, monkeypatch):
    c, app = admin_client
    checksum = hashlib.sha256(b"source").hexdigest()
    job = await _job(
        app,
        source_type=SourceType.url,
        source_ref="https://youtu.be/dQw4w9WgXcQ",
        input_checksum=checksum,
    )
    _source(app, job)
    await _fail(app, job, JobStage.publishing, ErrorCode.PUBLISH_FAILED)

    async def complete(_ctx, *, allow_receipt):
        return "COMPLETED"

    monkeypatch.setattr(admin_module, "completion_evidence", complete)
    monkeypatch.setattr(
        "shizzle_server.orchestrator.recover_publication.load_and_verify_package",
        lambda _path: SimpleNamespace(handoff={"source": {
            "sha256": checksum,
            "object_key": cloud.source_key(track_id_for_job(job.id)),
        }}),
    )
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["action"] == "recovered"
    assert response.json()["status"] == "verifying"
    assert response.json()["newJobId"] is None
    assert (await app.state.job_repo.get_job(job.id)).dismissed_at is None


async def test_publication_precondition_failure_falls_back_and_records_reason(
    admin_client, monkeypatch
):
    c, app = admin_client
    job = await _job(app, input_checksum="a" * 64)
    _source(app, job)
    await _fail(app, job, JobStage.publishing, ErrorCode.PUBLISH_FAILED)

    async def incomplete(_ctx, *, allow_receipt):
        raise ValueError("provider is not complete")

    monkeypatch.setattr(admin_module, "completion_evidence", incomplete)

    class FakeRunPod:
        async def cancel(self, _provider_id):
            return None

    monkeypatch.setattr(admin_module, "_runpod_for", lambda _settings: FakeRunPod())
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["resumeStage"] == "pending"
    event = next(e for e in await app.state.job_repo.list_events(job.id) if e.event == "admin_retry_started")
    assert "provider is not complete" in event.detail["fallback_reason"]


async def test_retry_refuses_when_runpod_cannot_be_confirmed_stopped(
    admin_client, session_factory, monkeypatch
):
    c, app = admin_client
    job = await _job(app)
    _source(app, job)
    await _fail(app, job, JobStage.dispatched)
    async with session_factory() as session, session.begin():
        row = await session.get(Job, job.id)
        row.runpod_job_id = "rp-uncertain"
        row.worker_phase = None

    class FailingRunPod:
        async def cancel(self, _provider_id):
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(admin_module, "_runpod_for", lambda _settings: FailingRunPod())
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 409
    assert "could not be confirmed stopped" in response.json()["detail"]
    assert (await app.state.job_repo.get_job(job.id)).status == JobStage.failed
    assert "admin_retry_cancel_failed" in [
        event.event for event in await app.state.job_repo.list_events(job.id)
    ]


async def test_dismiss_restore_visibility_workdir_and_retry_guard(admin_client):
    c, app = admin_client
    job = await _job(app)
    workdir = _source(app, job)
    await _fail(app, job, JobStage.downloading)

    dismissed = await c.post(f"/api/admin/jobs/{job.id}/dismiss", headers=HEADERS)
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["dismissedAt"] is not None
    assert not workdir.exists()
    assert (await c.get("/api/jobs")).json()["jobs"] == []
    default = (await c.get("/api/admin/jobs", headers=HEADERS)).json()
    assert default["jobs"] == [] and default["dismissedCount"] == 1
    assert default["counts"].get("failed", 0) == 0
    only = (await c.get("/api/admin/jobs?dismissed=only", headers=HEADERS)).json()
    assert len(only["jobs"]) == 1 and only["jobs"][0]["dismissedAt"] is not None
    retry = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert retry.status_code == 409 and "Restore" in retry.json()["detail"]

    restored = await c.post(f"/api/admin/jobs/{job.id}/restore", headers=HEADERS)
    assert restored.status_code == 200 and restored.json()["dismissedAt"] is None
    assert len((await c.get("/api/admin/jobs", headers=HEADERS)).json()["jobs"]) == 1
    events = [event.event for event in await app.state.job_repo.list_events(job.id)]
    assert "admin_dismissed" in events and "admin_restored" in events


async def test_track_remove_restore_and_deleted_filters(admin_client, session_factory):
    c, app = admin_client
    job = await _job(app)
    track_id = track_id_for_job(job.id)
    async with session_factory() as session, session.begin():
        session.add(Track(
            id=track_id,
            title="Library track",
            artist="Artist",
            duration_seconds=30,
            s3_prefix=f"tracks/{track_id}/1",
            manifest_key=f"tracks/{track_id}/1/manifest.json",
            generation=1,
        ))
        row = await session.get(Job, job.id)
        row.track_id = track_id
        row.status = JobStage.ready

    listed = (await c.get("/api/admin/tracks", headers=HEADERS)).json()["tracks"]
    assert len(listed) == 1 and listed[0]["jobId"] == job.id.hex
    removed = await c.post(f"/api/admin/tracks/{track_id}/remove", headers=HEADERS)
    assert removed.status_code == 200 and removed.json()["deletedAt"] is not None
    assert (await c.get("/api/admin/tracks", headers=HEADERS)).json()["tracks"] == []
    deleted = (await c.get("/api/admin/tracks?deleted=only", headers=HEADERS)).json()["tracks"]
    assert len(deleted) == 1 and deleted[0]["deletedAt"] is not None
    restored = await c.post(f"/api/admin/tracks/{track_id}/restore", headers=HEADERS)
    assert restored.status_code == 200 and restored.json()["deletedAt"] is None
    ledger = await app.state.track_repo.list_generation_events(track_id)
    assert [event.event for event in ledger] == ["admin_removed", "admin_restored"]
    assert all(event.detail["s3_objects_deleted"] is False for event in ledger)


async def test_bulk_actions_return_per_job_results_and_continue(admin_client, session_factory):
    c, app = admin_client
    failed = await _job(app)
    _source(app, failed)
    await _fail(app, failed, JobStage.pending)
    live = await _job(app)

    retried = await c.post(
        "/api/admin/jobs/bulk",
        headers=HEADERS,
        json={"action": "retry", "jobIds": [live.id.hex, failed.id.hex]},
    )
    assert retried.status_code == 200
    results = retried.json()
    assert results[0]["ok"] is False and "Only a failed job" in results[0]["reason"]
    assert results[1]["ok"] is True and results[1]["resumeStage"] == "pending"

    dismiss_me = await _job(app)
    async with session_factory() as session, session.begin():
        row = await session.get(Job, dismiss_me.id)
        row.status = JobStage.failed
    dismissed = await c.post(
        "/api/admin/jobs/bulk",
        headers=HEADERS,
        json={"action": "dismiss", "jobIds": [live.id.hex, dismiss_me.id.hex]},
    )
    assert dismissed.status_code == 200
    results = dismissed.json()
    assert results[0]["ok"] is False and results[1]["ok"] is True


async def test_retry_refuses_unresolved_reservation_and_fences_old_receipts(
    admin_client,
):
    c, app = admin_client
    job = await _job(app)
    _source(app, job)
    await _claim_and_advance(app, job, JobStage.dispatched)
    assert await app.state.job_repo.reserve_dispatch(
        job.id, worker_id="action-test", idempotency_key=f"{job.id.hex}:0"
    )
    await app.state.job_repo.fail_job(
        job.id,
        worker_id="action-test",
        error_code=ErrorCode.RUNPOD_TIMEOUT,
        error_detail="submit may have been accepted",
    )
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 409
    assert "unresolved" in response.json()["detail"]
    assert (await app.state.job_repo.get_job(job.id)).status == JobStage.failed
    assert "admin_retry_started" not in [
        event.event for event in await app.state.job_repo.list_events(job.id)
    ]

    # An append-only retry boundary prevents an older reservation/receipt
    # from becoming authoritative for the reopened attempt.
    events = await app.state.job_repo.list_events(job.id)
    events.append(SimpleNamespace(
        id=10_000,
        event="admin_retry_started",
        detail={"new_attempt": 1, "resume_stage": "pending"},
    ))
    assert pending_runpod_dispatch(events) is None
    job.attempt = 1
    events.append(SimpleNamespace(
        id=10_001,
        event="runpod_dispatch_recovered",
        detail={"idempotency_key": f"{job.id.hex}:0", "source": "handoff.json"},
    ))
    assert admin_module._selected_dispatch_key(job, events) == f"{job.id.hex}:1"


@pytest.mark.parametrize("rejection", ["http_409", "not_configured"])
async def test_terminal_dispatch_rejection_retries_with_real_fresh_key_fence(
    admin_client, monkeypatch, rejection
):
    """A real handle_dispatched rejection resolves its exact reservation."""
    from shizzle_server.orchestrator.runpod_client import (
        HttpRunPodClient,
        NotConfiguredRunPodClient,
    )
    from shizzle_server.orchestrator.stages import StageContext, handle_dispatched

    c, app = admin_client
    job = await _job(app)
    _source(app, job)
    claimed = await _claim_and_advance(app, job, JobStage.dispatched)
    if rejection == "http_409":
        runpod = HttpRunPodClient(
            api_key="test",
            endpoint_id="endpoint",
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(409)
            ),
        )
    else:
        runpod = NotConfiguredRunPodClient()
    ctx = StageContext(
        job=claimed,
        settings=app.state.settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=app.state.job_repo,
        runpod=runpod,
        worker_id="action-test",
    )
    with pytest.raises(StageError) as raised:
        await handle_dispatched(ctx)
    assert raised.value.provider_accepted is False
    await app.state.job_repo.fail_job(
        job.id,
        worker_id="action-test",
        error_code=raised.value.code,
        error_detail=raised.value.detail,
        provider_accepted=raised.value.provider_accepted,
    )

    async def inspection_must_not_run(*_args, **_kwargs):
        raise AssertionError("definitive provider rejection must not inspect S3")

    monkeypatch.setattr(
        cloud, "inspect_dispatch_attempt", inspection_must_not_run
    )
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 200, response.text
    events = await app.state.job_repo.list_events(job.id)
    failure = next(event for event in events if event.event == "failed")
    assert failure.detail["provider_accepted"] is False
    retry = next(event for event in events if event.event == "admin_retry_started")
    assert retry.detail["reservation_resolution"] == "provider_rejected"

    # Exercise the real SQL selectors: the retry boundary hides the old key,
    # the fresh key reserves, and neither before nor after that reservation
    # can a late confirmation attach the old provider execution.
    next_job = await app.state.job_repo.claim_next(
        worker_id="fresh-dispatch", lease_seconds=60
    )
    assert next_job is not None and next_job.attempt == 1
    for from_stage, to_stage in (
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.dispatched),
    ):
        next_job = await app.state.job_repo.advance(
            job.id,
            worker_id="fresh-dispatch",
            from_stage=from_stage,
            to_stage=to_stage,
        )
    old_key = f"{job.id.hex}:0"
    assert await app.state.job_repo.count_runpod_dispatches() == 0
    with pytest.raises(InvalidTransition):
        await app.state.job_repo.confirm_dispatch(
            job.id, idempotency_key=old_key, runpod_job_id="rp-late-old"
        )
    new_key = f"{job.id.hex}:1"
    assert await app.state.job_repo.reserve_dispatch(
        job.id, worker_id="fresh-dispatch", idempotency_key=new_key
    )
    with pytest.raises(InvalidTransition):
        await app.state.job_repo.confirm_dispatch(
            job.id, idempotency_key=old_key, runpod_job_id="rp-late-old"
        )
    assert await app.state.job_repo.confirm_dispatch(
        job.id, idempotency_key=new_key, runpod_job_id="rp-fresh"
    )
    assert (await app.state.job_repo.get_job(job.id)).runpod_job_id == "rp-fresh"

    # A delayed preflight from attempt 0 cannot reopen this newer confirmed
    # execution after it fails: its reservation token is now stale/absent.
    await app.state.job_repo.fail_job(
        job.id,
        worker_id="fresh-dispatch",
        error_code=ErrorCode.RUNPOD_DISPATCH_FAILED,
        error_detail="RunPod returned HTTP 401",
    )
    with pytest.raises(InvalidTransition):
        await app.state.job_repo.admin_restart_job(
            job.id,
            reason="stale delayed retry",
            runpod_outcome=retry.detail["runpod"],
        )
    current = await app.state.job_repo.get_job(job.id)
    assert current.status == JobStage.failed
    assert current.attempt == 1 and current.runpod_job_id == "rp-fresh"


@pytest.mark.parametrize(
    "detail",
    [
        "RunPod returned HTTP 409",
        admin_module.NotConfiguredRunPodClient._DETAIL,
    ],
)
async def test_legacy_exact_terminal_rejection_retries_without_structured_field(
    admin_client, monkeypatch, detail
):
    c, app = admin_client
    job, _key, _pending = await _fail_with_pending(
        app, code=ErrorCode.RUNPOD_DISPATCH_FAILED, detail=detail
    )

    async def inspection_must_not_run(*_args, **_kwargs):
        raise AssertionError("legacy exact rejection must not inspect S3")

    monkeypatch.setattr(cloud, "inspect_dispatch_attempt", inspection_must_not_run)
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 200, response.text
    retry = next(
        event
        for event in await app.state.job_repo.list_events(job.id)
        if event.event == "admin_retry_started"
    )
    assert retry.detail["reservation_resolution"] == "provider_rejected"


async def test_unconfirmed_reservation_inside_window_reports_retryable_time(
    admin_client,
):
    c, app = admin_client
    job, _key, _pending = await _fail_with_pending(app)
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "unresolved" in detail and "available after" in detail
    assert "Dismiss is available" in detail
    assert (await app.state.job_repo.get_job(job.id)).status == JobStage.failed


async def test_empty_attempt_after_deadline_retries_and_cancel_is_best_effort(
    admin_client, session_factory, monkeypatch
):
    c, app = admin_client
    job, _key, pending = await _fail_with_pending(app)
    async with session_factory() as session, session.begin():
        row = await session.get(Job, job.id)
        row.runpod_job_id = "rp-uncertain"
    await _age_reservation(session_factory, pending, app.state.settings)

    class CancelUnavailable:
        async def cancel(self, provider_id):
            assert provider_id == "rp-uncertain"
            raise RuntimeError("provider unavailable")

    monkeypatch.setattr(
        admin_module, "_runpod_for", lambda _settings: CancelUnavailable()
    )
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 200, response.text
    retry = next(
        event
        for event in await app.state.job_repo.list_events(job.id)
        if event.event == "admin_retry_started"
    )
    assert retry.detail["reservation_resolution"] == "no_receipt_after_deadline"
    assert retry.detail["runpod"]["provider_cancel"]["status"] == "cancel_failed"


async def test_receipt_after_deadline_refuses_retry(admin_client, session_factory, monkeypatch):
    c, app = admin_client
    job, _key, pending = await _fail_with_pending(app)
    await _age_reservation(session_factory, pending, app.state.settings)

    async def receipt(_ctx, *, idempotency_key):
        return cloud.DispatchAttemptInspection(
            receipt_present=True,
            runpod_job_id="rp-worker-ran",
            handoff_present=False,
        )

    monkeypatch.setattr(cloud, "inspect_dispatch_attempt", receipt)
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 409
    assert "dispatch.json receipt" in response.json()["detail"]
    assert "Dismiss is available" in response.json()["detail"]
    assert (await app.state.job_repo.get_job(job.id)).status == JobStage.failed


async def test_storage_error_refuses_with_storage_reason(admin_client, monkeypatch):
    c, app = admin_client
    job, _key, _pending = await _fail_with_pending(app)

    async def storage_error(_ctx, *, idempotency_key):
        raise StageError(
            ErrorCode.S3_UPLOAD_FAILED,
            "dispatch retry read failed: storage unavailable",
            retryable=True,
        )

    monkeypatch.setattr(cloud, "inspect_dispatch_attempt", storage_error)
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 409
    assert "Storage could not be checked" in response.json()["detail"]
    assert "storage unavailable" in response.json()["detail"]
    assert "Dismiss is available" in response.json()["detail"]


async def test_missing_source_precedes_reservation_storage_check(
    admin_client, monkeypatch
):
    c, app = admin_client
    job, _key, _pending = await _fail_with_pending(app)
    (app.state.settings.data_dir / job.id.hex / "source.mp4").unlink()

    async def inspection_must_not_run(*_args, **_kwargs):
        raise AssertionError("source check must come first")

    monkeypatch.setattr(cloud, "inspect_dispatch_attempt", inspection_must_not_run)
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 409
    assert "source no longer exists" in response.json()["detail"]
    assert "Dismiss is available" in response.json()["detail"]


async def test_unconfirmed_valid_handoff_recovers_and_selects_exact_prefix(
    admin_client, monkeypatch
):
    c, app = admin_client
    checksum = hashlib.sha256(b"source").hexdigest()
    job, key, _pending = await _fail_with_pending(app, input_checksum=checksum)
    shared_package = app.state.settings.data_dir / job.id.hex / "package"
    _write_real_lossless_package(shared_package, job, checksum)
    first_stem = shared_package / "stems" / "vocals.wav"
    fresh_first_stem = first_stem.read_bytes()
    stale_bytes = bytearray(fresh_first_stem)
    stale_bytes[-1] ^= 1  # same size, different bytes and sha256
    first_stem.write_bytes(stale_bytes)
    stale_handoff = json.loads((shared_package / "handoff.json").read_text())
    stale_handoff["stems"][0]["sha256"] = hashlib.sha256(stale_bytes).hexdigest()
    (shared_package / "handoff.json").write_text(json.dumps(stale_handoff))

    async def handoff(_ctx, *, idempotency_key):
        assert idempotency_key == key
        return cloud.DispatchAttemptInspection(
            receipt_present=False, runpod_job_id=None, handoff_present=True
        )

    def download(_s3, _bucket, prefix, destination):
        assert prefix == cloud.attempt_prefix(track_id_for_job(job.id), key)
        if destination == shared_package and (destination / "handoff.json").exists():
            return  # exposes stale-cache reuse unless verification cleans it
        _write_real_lossless_package(destination, job, checksum)

    monkeypatch.setattr(cloud, "inspect_dispatch_attempt", handoff)
    monkeypatch.setattr(admin_module, "download_package", download)
    monkeypatch.setattr(cloud, "download_package", download)
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["action"] == "recovered"
    assert response.json()["status"] == "verifying"
    assert response.json()["attempt"] == 0
    current = await app.state.job_repo.get_job(job.id)
    assert current.status == JobStage.verifying
    assert current.runpod_job_id is None
    events = await app.state.job_repo.list_events(job.id)
    retry = next(event for event in events if event.event == "admin_retry_started")
    assert retry.detail["reservation_resolution"] == "handoff"
    assert retry.detail["dispatch_key"] == key

    # Verification consumes the recovery-selected immutable attempt, not the
    # legacy base prefix, and re-proves the real package bytes.
    from shizzle_server.orchestrator.stages import StageContext, handle_verifying

    app.state.settings.shizzle_pipeline = "cloud"
    ctx = StageContext(
        job=current,
        settings=app.state.settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=app.state.job_repo,
        runpod=admin_module._runpod_for(app.state.settings),
        worker_id="verify-handoff-recovery",
    )
    assert await handle_verifying(ctx) == JobStage.publishing
    assert ctx.detail["package"]["sample_count"] == 4410
    assert first_stem.read_bytes() == fresh_first_stem


async def test_invalid_handoff_preflight_is_actionable_409(
    admin_client, monkeypatch
):
    c, app = admin_client
    checksum = hashlib.sha256(b"source").hexdigest()
    job, _key, _pending = await _fail_with_pending(app, input_checksum=checksum)

    async def handoff(_ctx, *, idempotency_key):
        return cloud.DispatchAttemptInspection(
            receipt_present=False, runpod_job_id=None, handoff_present=True
        )

    def invalid_package(*_args, **_kwargs):
        raise ValueError("invalid selected package")

    monkeypatch.setattr(cloud, "inspect_dispatch_attempt", handoff)
    monkeypatch.setattr(admin_module, "download_package", invalid_package)
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 409, response.text
    assert "publication recovery preflight failed" in response.json()["detail"]
    assert "invalid selected package" in response.json()["detail"]
    assert "Dismiss is available" in response.json()["detail"]
    assert any(
        event.event == "admin_retry_refused"
        for event in await app.state.job_repo.list_events(job.id)
    )


async def test_handoff_preflight_cannot_recreate_dismissed_workdir(
    admin_client, monkeypatch
):
    import asyncio
    import threading

    c, app = admin_client
    checksum = hashlib.sha256(b"source").hexdigest()
    job, _key, _pending = await _fail_with_pending(app, input_checksum=checksum)

    async def handoff(_ctx, *, idempotency_key):
        return cloud.DispatchAttemptInspection(
            receipt_present=False, runpod_job_id=None, handoff_present=True
        )

    started = threading.Event()
    resume = threading.Event()

    def paused_download(_s3, _bucket, prefix, destination):
        started.set()
        assert resume.wait(timeout=5)
        _write_real_lossless_package(destination, job, checksum)

    monkeypatch.setattr(cloud, "inspect_dispatch_attempt", handoff)
    monkeypatch.setattr(admin_module, "download_package", paused_download)
    retry_task = asyncio.create_task(
        c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    )
    assert await asyncio.to_thread(started.wait, 5)
    dismissed = await c.post(f"/api/admin/jobs/{job.id}/dismiss", headers=HEADERS)
    assert dismissed.status_code == 200, dismissed.text
    resume.set()
    refused = await retry_task
    assert refused.status_code == 409
    assert not (app.state.settings.data_dir / job.id.hex).exists()
    assert not any(
        path.name.startswith(f".retry-{job.id.hex}-")
        for path in app.state.settings.data_dir.iterdir()
    )


async def test_cancelled_handoff_preflight_waits_for_thread_before_cleanup(
    admin_client, monkeypatch
):
    import asyncio
    import threading

    c, app = admin_client
    checksum = hashlib.sha256(b"source").hexdigest()
    job, _key, _pending = await _fail_with_pending(app, input_checksum=checksum)

    async def handoff(_ctx, *, idempotency_key):
        return cloud.DispatchAttemptInspection(
            receipt_present=False, runpod_job_id=None, handoff_present=True
        )

    started = threading.Event()
    resume = threading.Event()

    def paused_download(_s3, _bucket, _prefix, destination):
        started.set()
        assert resume.wait(timeout=5)
        _write_real_lossless_package(destination, job, checksum)

    monkeypatch.setattr(cloud, "inspect_dispatch_attempt", handoff)
    monkeypatch.setattr(admin_module, "download_package", paused_download)
    retry_task = asyncio.create_task(
        c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    )
    assert await asyncio.to_thread(started.wait, 5)
    retry_task.cancel()
    await asyncio.sleep(0)
    assert not retry_task.done()  # the S3 thread is still writing
    resume.set()
    with pytest.raises(asyncio.CancelledError):
        await retry_task
    assert not any(
        path.name.startswith(f".retry-{job.id.hex}-")
        for path in app.state.settings.data_dir.iterdir()
    )


async def test_duplicate_keep_revalidates_stale_keeper_before_cleanup(
    admin_client, session_factory
):
    c, app = admin_client
    checksum = "d" * 64
    keeper = await _job(app, title="Keeper", input_checksum=checksum)
    alternative = await _job(app, title="Alternative", input_checksum=checksum)
    failed = await _job(app, title="Failed", input_checksum=checksum)
    keeper_id = await _linked_track(session_factory, keeper, title="Keeper", created_seconds=2)
    alternative_id = await _linked_track(
        session_factory, alternative, title="Alternative", created_seconds=1
    )
    await _fail(app, failed, JobStage.pending)
    groups = (await c.get("/api/admin/duplicates", headers=HEADERS)).json()["groups"]
    assert groups[0]["keeperTrackId"] == str(keeper_id)
    displayed = groups[0]

    # The displayed keeper was removed before the operator clicked Keep.
    removed = await c.post(f"/api/admin/tracks/{keeper_id}/remove", headers=HEADERS)
    assert removed.status_code == 200
    refused = await c.post(
        "/api/admin/duplicates/action",
        headers=HEADERS,
        json={
            "action": "keep",
            "jobIds": [member["id"] for member in displayed["jobs"]],
            "trackIds": [member["id"] for member in displayed["tracks"]],
            "keeperTrackId": str(keeper_id),
        },
    )
    assert refused.status_code == 409
    assert "keeper" in refused.json()["detail"].lower()
    assert (await app.state.track_repo.get(alternative_id)).deleted_at is None
    assert (await app.state.job_repo.get_job(failed.id)).dismissed_at is None


async def test_node_backed_failure_expires_assignment_without_runpod_cancel(
    admin_client, session_factory, monkeypatch
):
    c, app = admin_client
    await app.state.node_repo.heartbeat("mac-mini", capabilities={"separate": True})
    job = await _job(app)
    _source(app, job)
    claimed = await app.state.job_repo.claim_next(worker_id="action-test", lease_seconds=600)
    assert claimed is not None
    assignment = await app.state.node_repo.create_assignment(
        job.id,
        worker_id="action-test",
        node_id="mac-mini",
        kind=NodeAssignmentKind.separate,
        payload={},
        idempotency_key=f"{job.id.hex}:0",
    )
    assert assignment is not None
    await app.state.job_repo.advance(
        job.id, worker_id="action-test", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await app.state.job_repo.advance(
        job.id, worker_id="action-test", from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    async with session_factory() as session, session.begin():
        row = await session.get(Job, job.id)
        row.runpod_job_id = f"node:{assignment.id}"
    await app.state.job_repo.fail_job(
        job.id,
        worker_id="action-test",
        error_code=ErrorCode.NODE_FAILED,
        error_detail="node failed",
    )

    class NoRunPodCancel:
        async def cancel(self, _provider_id):
            raise AssertionError("node identity must not reach RunPod cancellation")

    monkeypatch.setattr(admin_module, "_runpod_for", lambda _settings: NoRunPodCancel())
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert (await app.state.node_repo.get_assignment(assignment.id)).status == NodeAssignmentStatus.expired
    assert (await app.state.node_repo.get("mac-mini")).busy_assignment_id is None


async def test_failed_node_identity_does_not_hide_later_runpod_rejection(
    admin_client, session_factory, monkeypatch
):
    c, app = admin_client
    job, _key, _pending = await _fail_with_pending(
        app,
        code=ErrorCode.RUNPOD_DISPATCH_FAILED,
        detail="RunPod returned HTTP 409",
        provider_accepted=False,
    )
    async with session_factory() as session, session.begin():
        row = await session.get(Job, job.id)
        row.runpod_job_id = "node:failed-predecessor"
        row.worker_phase = "failed"

    class NoNodeCancellation:
        async def cancel(self, _provider_id):
            raise AssertionError("failed node identity must not reach RunPod cancel")

    monkeypatch.setattr(
        admin_module, "_runpod_for", lambda _settings: NoNodeCancellation()
    )
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 200, response.text
    retry = next(
        event
        for event in await app.state.job_repo.list_events(job.id)
        if event.event == "admin_retry_started"
    )
    assert retry.detail["reservation_resolution"] == "provider_rejected"


@pytest.mark.parametrize(
    ("stage", "code"),
    [
        (JobStage.verifying, ErrorCode.CHECKSUM_MISMATCH),
        (JobStage.publishing, ErrorCode.PUBLISH_FAILED),
    ],
)
async def test_publication_invalid_assets_fall_back_to_source_checked_restart(
    admin_client, monkeypatch, stage, code
):
    c, app = admin_client
    job = await _job(app, input_checksum="a" * 64)
    _source(app, job)
    await _fail(app, job, stage, code)

    async def completed(_ctx, *, allow_receipt):
        return "COMPLETED"

    monkeypatch.setattr(admin_module, "completion_evidence", completed)

    def invalid_package(_path):
        raise FileNotFoundError("required package bytes are unavailable")

    monkeypatch.setattr(
        "shizzle_server.orchestrator.recover_publication.load_and_verify_package",
        invalid_package,
    )

    class FakeRunPod:
        async def cancel(self, _provider_id):
            return None

    monkeypatch.setattr(admin_module, "_runpod_for", lambda _settings: FakeRunPod())
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["resumeStage"] == "pending"
    event = next(e for e in await app.state.job_repo.list_events(job.id) if e.event == "admin_retry_started")
    assert "required package bytes" in event.detail["fallback_reason"]


@pytest.mark.parametrize(
    ("stage", "code"),
    [
        (JobStage.verifying, ErrorCode.CHECKSUM_MISMATCH),
        (JobStage.publishing, ErrorCode.PUBLISH_FAILED),
    ],
)
async def test_publication_fallback_reason_survives_source_refusal(
    admin_client, monkeypatch, stage, code
):
    c, app = admin_client
    job = await _job(app, input_checksum="a" * 64)
    await _fail(app, job, stage, code)

    async def completed(_ctx, *, allow_receipt):
        return "COMPLETED"

    monkeypatch.setattr(admin_module, "completion_evidence", completed)
    monkeypatch.setattr(
        "shizzle_server.orchestrator.recover_publication.load_and_verify_package",
        lambda _path: (_ for _ in ()).throw(
            FileNotFoundError("required package bytes are unavailable")
        ),
    )
    response = await c.post(f"/api/admin/jobs/{job.id}/retry", headers=HEADERS)
    assert response.status_code == 409
    event = next(
        event
        for event in await app.state.job_repo.list_events(job.id)
        if event.event == "admin_retry_refused"
    )
    assert "required package bytes" in event.detail["fallback_reason"]


async def test_failed_url_job_resubmits_as_new_job_and_dismisses_old(admin_client):
    c, app = admin_client
    old = await _job(
        app,
        source_type=SourceType.url,
        source_ref="https://youtu.be/dQw4w9WgXcQ?token=server-only",
        title="Link job",
        artist="Artist",
    )
    await _fail(app, old, JobStage.downloading)
    response = await c.post(f"/api/admin/jobs/{old.id}/retry", headers=HEADERS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "resubmitted" and body["action"] == "resubmitted"
    assert body["newJobId"] != old.id.hex
    assert "server-only" not in response.text and "source_ref" not in response.text
    new_id = uuid.UUID(body["newJobId"])
    old_now = await app.state.job_repo.get_job(old.id)
    new = await app.state.job_repo.get_job(new_id)
    assert old_now.dismissed_at is not None and old_now.status == JobStage.failed
    assert new.status == JobStage.pending and new.id != old.id
    assert new.source_ref == old.source_ref and new.title == "Link job" and new.artist == "Artist"
    old_events = [event.event for event in await app.state.job_repo.list_events(old.id)]
    new_events = [event.event for event in await app.state.job_repo.list_events(new.id)]
    assert "admin_resubmitted" in old_events and "admin_dismissed" in old_events
    assert "admin_resubmitted_from" in new_events and "created" in new_events


async def test_retry_winner_dismissal_refusal_preserves_source(admin_client):
    c, app = admin_client
    # When retry wins first, dismissal is refused before it can remove the
    # reopened job's source.
    other = await _job(app)
    other_dir = _source(app, other)
    await _fail(app, other, JobStage.pending)
    retried = await c.post(f"/api/admin/jobs/{other.id}/retry", headers=HEADERS)
    assert retried.status_code == 200
    refused = await c.post(f"/api/admin/jobs/{other.id}/dismiss", headers=HEADERS)
    assert refused.status_code == 409
    assert (other_dir / "source.mp4").is_file()


async def test_bulk_dismiss_cleanup_failure_is_actionable_and_continues(
    admin_client, monkeypatch
):
    c, app = admin_client
    first = await _job(app)
    second = await _job(app)
    _source(app, first)
    _source(app, second)
    await _fail(app, first, JobStage.pending)
    await _fail(app, second, JobStage.pending)
    calls = 0
    real_rmtree = admin_module.shutil.rmtree

    def flaky_remove(_path):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise PermissionError("directory is locked by another process")
        real_rmtree(_path)

    monkeypatch.setattr(admin_module.shutil, "rmtree", flaky_remove)
    response = await c.post(
        "/api/admin/jobs/bulk",
        headers=HEADERS,
        json={"action": "dismiss", "jobIds": [first.id.hex, second.id.hex]},
    )
    assert response.status_code == 200
    results = response.json()
    assert results[0]["ok"] is False and "cleanup failed" in results[0]["reason"]
    assert results[1]["ok"] is True
    assert (await app.state.job_repo.get_job(first.id)).dismissed_at is None
    assert (await app.state.job_repo.get_job(second.id)).dismissed_at is not None


def test_admin_retry_is_not_a_general_state_machine_transition():
    assert JobStage.pending not in ALLOWED_TRANSITIONS[JobStage.failed]


async def test_duplicate_grouping_rules_and_url_redaction(admin_client):
    c, app = admin_client
    checksum = "a" * 64
    await _job(app, title="Checksum one", input_checksum=checksum)
    await _job(app, title="Checksum two", input_checksum=checksum)
    await _job(
        app,
        source_type=SourceType.url,
        source_ref="https://www.youtube.com/watch?v=dQw4w9WgXcQ&token=secret-one",
        title="Video one",
    )
    await _job(
        app,
        source_type=SourceType.url,
        source_ref="https://youtu.be/dQw4w9WgXcQ?token=secret-two",
        title="Video two",
    )
    await _job(app, title="  The   Song!!! ", artist="  ARTIST ")
    await _job(app, title="the song", artist="artist")

    response = await c.get("/api/admin/duplicates", headers=HEADERS)
    assert response.status_code == 200
    groups = response.json()["groups"]
    assert len(groups) == 3
    identities = {group["identity"] for group in groups}
    assert f"checksum:{checksum}" in identities
    assert "youtube:dQw4w9WgXcQ" in identities
    assert "title:the song artist" in identities
    assert "secret-one" not in response.text and "secret-two" not in response.text
    assert "source_ref" not in response.text


async def _linked_track(session_factory, job, *, title, created_seconds, deleted=False):
    track_id = track_id_for_job(job.id)
    async with session_factory() as session, session.begin():
        session.add(Track(
            id=track_id,
            title=title,
            artist="Artist",
            duration_seconds=30,
            s3_prefix=f"tracks/{track_id}/1",
            manifest_key=f"tracks/{track_id}/1/manifest.json",
            generation=1,
            created_at=utcnow() + timedelta(seconds=created_seconds),
            deleted_at=utcnow() if deleted else None,
        ))
        row = await session.get(Job, job.id)
        row.track_id = track_id
        row.status = JobStage.ready
    return track_id


async def test_title_only_group_does_not_claim_a_unique_checksum_is_shared(
    admin_client,
):
    c, app = admin_client
    await _job(app, title="Same Song", artist="Artist", input_checksum="1" * 64)
    await _job(app, title="same song!", artist="artist", input_checksum="2" * 64)
    groups = (await c.get("/api/admin/duplicates", headers=HEADERS)).json()["groups"]
    assert len(groups) == 1
    assert groups[0]["identity"] == "title:same song artist"


async def test_single_ready_job_and_its_track_are_not_a_duplicate_group(
    admin_client, session_factory
):
    c, app = admin_client
    job = await _job(app, title="One song", input_checksum="d" * 64)
    await _linked_track(session_factory, job, title="One song", created_seconds=0)
    groups = (await c.get("/api/admin/duplicates", headers=HEADERS)).json()["groups"]
    assert groups == []


async def test_two_ready_jobs_same_file_form_one_two_item_group(
    admin_client, session_factory
):
    c, app = admin_client
    checksum = "e" * 64
    first = await _job(app, title="Same recording", input_checksum=checksum)
    second = await _job(app, title="Same recording", input_checksum=checksum)
    first_id = await _linked_track(
        session_factory, first, title="Same recording", created_seconds=1
    )
    second_id = await _linked_track(
        session_factory, second, title="Same recording", created_seconds=2
    )
    groups = (await c.get("/api/admin/duplicates", headers=HEADERS)).json()["groups"]
    assert len(groups) == 1
    assert groups[0]["jobs"] == []
    assert {member["id"] for member in groups[0]["tracks"]} == {
        str(first_id), str(second_id)
    }


async def test_failed_job_plus_different_ready_item_is_a_duplicate_group(
    admin_client, session_factory
):
    c, app = admin_client
    checksum = "f" * 64
    ready = await _job(app, title="Song", input_checksum=checksum)
    track_id = await _linked_track(
        session_factory, ready, title="Song", created_seconds=1
    )
    failed = await _job(app, title="Failed upload", input_checksum=checksum)
    await _fail(app, failed, JobStage.pending)
    groups = (await c.get("/api/admin/duplicates", headers=HEADERS)).json()["groups"]
    assert len(groups) == 1
    assert [member["id"] for member in groups[0]["jobs"]] == [failed.id.hex]
    assert [member["id"] for member in groups[0]["tracks"]] == [str(track_id)]


async def test_duplicate_keeper_choice_and_group_actions(admin_client, session_factory):
    c, app = admin_client
    checksum = "b" * 64
    older = await _job(app, title="Older", input_checksum=checksum)
    newer = await _job(app, title="Newer", input_checksum=checksum)
    failed = await _job(app, title="Failed", input_checksum=checksum)
    older_id = await _linked_track(session_factory, older, title="Older", created_seconds=1)
    newer_id = await _linked_track(session_factory, newer, title="Newer", created_seconds=2)
    await _fail(app, failed, JobStage.pending)

    groups = (await c.get("/api/admin/duplicates", headers=HEADERS)).json()["groups"]
    assert len(groups) == 1 and groups[0]["keeperTrackId"] == str(newer_id)
    assert [member["id"] for member in groups[0]["jobs"]] == [failed.id.hex]
    assert len(groups[0]["tracks"]) == 2
    result = await c.post(
        "/api/admin/duplicates/action",
        headers=HEADERS,
        json={
            "action": "keep",
            "jobIds": [failed.id.hex],
            "trackIds": [str(older_id), str(newer_id)],
            "keeperTrackId": str(newer_id),
        },
    )
    assert result.status_code == 200, result.text
    assert all(item["ok"] for item in result.json())
    assert (await app.state.job_repo.get_job(failed.id)).dismissed_at is not None
    assert (await app.state.track_repo.get(older_id)).deleted_at is not None
    assert (await app.state.track_repo.get(newer_id)).deleted_at is None

    other = await _job(app, title="Only failure", input_checksum="c" * 64)
    await _fail(app, other, JobStage.pending)
    no_keeper = await c.post(
        "/api/admin/duplicates/action",
        headers=HEADERS,
        json={"action": "dismiss_failed", "jobIds": [other.id.hex], "trackIds": []},
    )
    assert no_keeper.status_code == 200 and no_keeper.json()[0]["ok"] is True
    assert (await app.state.job_repo.get_job(other.id)).dismissed_at is not None

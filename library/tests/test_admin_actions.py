"""Administrator failed-job and library actions (G7-G9), fully offline."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from shizzle_server.api import admin as admin_module
from shizzle_server.api.media import _s3_client
from shizzle_server.db.models import (
    ALLOWED_TRANSITIONS,
    Job,
    JobStage,
    NodeAssignmentKind,
    NodeAssignmentStatus,
    SourceType,
    Track,
    utcnow,
)
from shizzle_server.db.repository import (
    pending_runpod_dispatch,
    track_id_for_job,
)
from shizzle_server.errors import ErrorCode
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
    assert response.json() == {"status": "pending", "resumeStage": "pending", "attempt": 1}
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

    present = await _job(app)
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

    # The displayed keeper was removed before the operator clicked Keep.
    removed = await c.post(f"/api/admin/tracks/{keeper_id}/remove", headers=HEADERS)
    assert removed.status_code == 200
    refused = await c.post(
        "/api/admin/duplicates/action",
        headers=HEADERS,
        json={
            "action": "keep",
            "jobIds": [keeper.id.hex, alternative.id.hex, failed.id.hex],
            "trackIds": [str(keeper_id), str(alternative_id)],
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


async def test_old_completed_download_does_not_satisfy_reopened_attempt(
    settings, job_repo, session_factory
):
    from shizzle_server.db.repository import NodeRepository
    from shizzle_server.orchestrator.nodes import download_via_node

    cloud_settings = settings
    cloud_settings.shizzle_pipeline = "cloud"
    cloud_settings.shizzle_node_routing = "prefer"
    node_repo = NodeRepository(session_factory)
    await node_repo.heartbeat("mac-mini", capabilities={"download": True, "separate": True})
    job = await job_repo.create_job(
        source_type=SourceType.url,
        source_ref="https://youtu.be/dQw4w9WgXcQ",
        title="Fresh download",
    )
    claimed = await job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed is not None
    await job_repo.advance(
        job.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    old = await node_repo.create_assignment(
        job.id,
        worker_id="w",
        node_id="mac-mini",
        kind=NodeAssignmentKind.download,
        payload={"attempt": 0},
    )
    assert old is not None
    await node_repo.acknowledge(old.id, node_id="mac-mini")
    await node_repo.complete(old.id, node_id="mac-mini", detail={"sha256": "a" * 64})
    async with session_factory() as session, session.begin():
        row = await session.get(Job, job.id)
        row.attempt = 1
        claimed = row
    ctx = SimpleNamespace(
        job=claimed,
        settings=cloud_settings,
        jobs=job_repo,
        worker_id="w",
        nodes=node_repo,
        runtime=None,
        detail={},
        park_seconds=None,
    )
    result = await download_via_node(ctx)
    assert result is None
    latest = await node_repo.latest_for_job(job.id, kind=NodeAssignmentKind.download)
    assert latest is not None and latest.id != old.id
    assert latest.payload["attempt"] == 1
    assert latest.status == NodeAssignmentStatus.assigned


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
    result = await c.post(
        "/api/admin/duplicates/action",
        headers=HEADERS,
        json={
            "action": "keep",
            "jobIds": [older.id.hex, newer.id.hex, failed.id.hex],
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

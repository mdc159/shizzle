"""Recovery claims preserve one provider submission and existing lease rules."""
import uuid
from datetime import timedelta

import pytest

from shizzle_server.db.models import Job, JobEvent, JobStage, SourceType, utcnow
from shizzle_server.db.repository import InvalidTransition
from shizzle_server.errors import ErrorCode


async def failed_publication(job_repo, session_factory):
    job = await job_repo.create_job(source_type=SourceType.upload, source_ref="source.mp4")
    async with session_factory() as session, session.begin():
        row = await session.get(Job, job.id)
        row.status = JobStage.failed
        row.error_code = ErrorCode.PUBLISH_FAILED.value
        row.runpod_job_id = "provider-completed"
        row.input_checksum = "a" * 64
        row.attempt = 2
        session.add(JobEvent(job_id=job.id, event="runpod_dispatched", detail={
            "runpod_job_id": "provider-completed", "idempotency_key": "actual-dispatch-key",
        }))
        session.add(JobEvent(job_id=job.id, event="failed", detail={"stage": "publishing"}))
    return await job_repo.get_job(job.id)


def recovery_args(job):
    return {
        "expected_provider_id": job.runpod_job_id,
        "expected_checksum": job.input_checksum,
        "expected_idempotency_key": job.idempotency_key,
        "expected_dispatch_key": "actual-dispatch-key",
        "recovery_id": uuid.uuid4(), "worker_id": "recovery-test", "lease_seconds": 120,
    }


async def test_recovery_claim_never_reopens_dispatch(job_repo, session_factory):
    job = await failed_publication(job_repo, session_factory)
    args = recovery_args(job)
    recovered = await job_repo.recover_publication(job.id, **args)
    assert recovered.status == JobStage.verifying
    assert recovered.runpod_job_id == job.runpod_job_id
    assert recovered.idempotency_key == job.idempotency_key
    assert recovered.attempt == job.attempt
    assert recovered.lease_owner == "recovery-test"
    assert recovered.lease_expires_at is not None
    assert recovered.error_code is None
    assert await job_repo.claim_next(worker_id="other", lease_seconds=120) is None
    events = await job_repo.list_events(job.id)
    assert events[-2].event == "failed"
    assert events[-1].event == "publication_recovery_started"
    assert events[-1].detail["recovery_id"] == str(args["recovery_id"])
    with pytest.raises(InvalidTransition):
        await job_repo.recover_publication(job.id, **args)


@pytest.mark.parametrize("field,value", [
    ("expected_provider_id", "wrong-provider"),
    ("expected_checksum", "b" * 64),
    ("expected_idempotency_key", "wrong-reservation"),
    ("expected_dispatch_key", "wrong-dispatch"),
])
async def test_changed_identity_cannot_recover(job_repo, session_factory, field, value):
    job = await failed_publication(job_repo, session_factory)
    args = recovery_args(job)
    args[field] = value
    with pytest.raises(InvalidTransition):
        await job_repo.recover_publication(job.id, **args)
    assert (await job_repo.get_job(job.id)).status == JobStage.failed
    assert len(await job_repo.list_events(job.id)) == 3


@pytest.mark.parametrize("field,value", [
    ("status", JobStage.dispatched), ("error_code", "RUNPOD_DISPATCH_FAILED"),
    ("lease_expires_at", "future"),
])
async def test_non_publication_or_active_lease_refused(job_repo, session_factory, field, value):
    job = await failed_publication(job_repo, session_factory)
    async with session_factory() as session, session.begin():
        row = await session.get(Job, job.id)
        setattr(row, field, utcnow() + timedelta(minutes=5) if value == "future" else value)
    with pytest.raises(InvalidTransition):
        await job_repo.recover_publication(job.id, **recovery_args(job))


async def test_latest_failure_must_be_publication(job_repo, session_factory):
    job = await failed_publication(job_repo, session_factory)
    await job_repo.append_event(job.id, "failed", {"stage": "verifying"})
    with pytest.raises(InvalidTransition):
        await job_repo.recover_publication(job.id, **recovery_args(job))


@pytest.mark.parametrize("provider_status", ["IN_QUEUE", "IN_PROGRESS", "FAILED", "UNKNOWN"])
async def test_command_never_claims_uncertain_provider(monkeypatch, provider_status):
    from argparse import Namespace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from shizzle_server.orchestrator import recover_publication as command

    job = SimpleNamespace(
        runpod_job_id="provider", input_checksum="a" * 64,
        status=JobStage.failed, error_code="PUBLISH_FAILED",
    )
    orchestrator = SimpleNamespace(
        settings=SimpleNamespace(cloud_pipeline=True),
        pipeline=None,
        jobs=SimpleNamespace(get_job=AsyncMock(return_value=job), recover_publication=AsyncMock()),
        runpod=SimpleNamespace(poll=AsyncMock(return_value={"status": provider_status})),
        engine=SimpleNamespace(dispose=AsyncMock()),
    )
    monkeypatch.setattr(command, "Orchestrator", lambda **_kwargs: orchestrator)
    args = Namespace(recovery_id=str(uuid.uuid4()), job_id=str(uuid.uuid4()),
                     provider_job_id="provider", input_checksum="a" * 64, execute=True,
                     use_completion_receipts=False)
    with pytest.raises(ValueError, match="completion is not confirmed"):
        await command.recover(args)
    orchestrator.jobs.recover_publication.assert_not_called()
    orchestrator.engine.dispose.assert_awaited_once()


@pytest.mark.parametrize("mismatch", [None, "provider", "prefix", "marker", "completion"])
async def test_provider_404_requires_matching_completion_receipts(monkeypatch, mismatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from shizzle_server.db.repository import track_id_for_job
    from shizzle_server.errors import StageError
    from shizzle_server.orchestrator import recover_publication as command

    job = SimpleNamespace(id=uuid.uuid4(), runpod_job_id="provider", idempotency_key="ingest-key")
    prefix = command.attempt_prefix(track_id_for_job(job.id), "actual-dispatch-key")
    events = [SimpleNamespace(event="stage_completed", detail={
        "from": "dispatched", "to": "verifying",
        "package_prefix": "wrong" if mismatch == "prefix" else prefix,
    })]
    events.append(SimpleNamespace(event="runpod_dispatched", detail={
        "runpod_job_id": "provider", "idempotency_key": "actual-dispatch-key",
    }))
    if mismatch != "completion":
        events.append(SimpleNamespace(event="worker_completed", detail={}))
    ctx = SimpleNamespace(job=job,
                          jobs=SimpleNamespace(list_events=AsyncMock(return_value=events)),
                          runpod=SimpleNamespace(poll=AsyncMock(side_effect=StageError(
                              ErrorCode.RUNPOD_DISPATCH_FAILED, "RunPod returned HTTP 404"))))
    monkeypatch.setattr(command, "accepted_dispatch_id", AsyncMock(
        return_value="wrong" if mismatch == "provider" else "provider"))
    monkeypatch.setattr(command, "package_ready", AsyncMock(return_value=mismatch != "marker"))
    if mismatch:
        with pytest.raises(ValueError, match="do not reconcile"):
            await command.completion_evidence(ctx, allow_receipt=True)
    else:
        assert await command.completion_evidence(ctx, allow_receipt=True) == "durable_receipts_after_provider_404"


@pytest.mark.parametrize("status,allow", [(404, False), (403, True), (502, True)])
async def test_provider_failures_never_silently_fall_back(status, allow):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from shizzle_server.errors import StageError
    from shizzle_server.orchestrator import recover_publication as command

    ctx = SimpleNamespace(job=SimpleNamespace(runpod_job_id="provider"),
                          jobs=SimpleNamespace(list_events=AsyncMock()),
                          runpod=SimpleNamespace(poll=AsyncMock(side_effect=StageError(
                              ErrorCode.RUNPOD_DISPATCH_FAILED, f"RunPod returned HTTP {status}"))))
    with pytest.raises(StageError):
        await command.completion_evidence(ctx, allow_receipt=allow)
    ctx.jobs.list_events.assert_not_called()

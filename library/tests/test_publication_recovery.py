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


@pytest.mark.parametrize("provider_status", ["IN_QUEUE", "IN_PROGRESS", "FAILED", "UNKNOWN", "COMPLETED"])
async def test_command_never_claims_uncertain_provider(monkeypatch, provider_status):
    from argparse import Namespace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from shizzle_server.orchestrator import recover_publication as command

    job = SimpleNamespace(
        id=uuid.uuid4(),
        runpod_job_id="provider", input_checksum="a" * 64,
        status=JobStage.failed, error_code="PUBLISH_FAILED",
    )
    orchestrator = SimpleNamespace(
        settings=SimpleNamespace(cloud_pipeline=True),
        pipeline=None,
        jobs=SimpleNamespace(get_job=AsyncMock(return_value=job), recover_publication=AsyncMock(),
                             list_events=AsyncMock(return_value=[SimpleNamespace(
                                 id=1, event="failed", detail={"stage": "publishing"})])),
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


@pytest.mark.parametrize("outcome", ["success", "failure", "interrupted", "existing_same", "existing_different"])
async def test_execute_uses_real_lease_loop_and_atomic_publication(
    job_repo, session_factory, settings, monkeypatch, outcome,
):
    import asyncio
    import hashlib
    from argparse import Namespace
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from shizzle_server.db.repository import track_id_for_job
    from shizzle_server.orchestrator import cloud
    from shizzle_server.orchestrator import recover_publication as command
    from shizzle_server.orchestrator.loop import Orchestrator
    from shizzle_server.publish.lossless_intake import IntakeError

    job = await failed_publication(job_repo, session_factory)
    source_bytes = b"verified test source"
    checksum = hashlib.sha256(source_bytes).hexdigest()
    track_id = track_id_for_job(job.id)
    prefix = cloud.attempt_prefix(track_id, "actual-dispatch-key")
    async with session_factory() as session, session.begin():
        row = await session.get(Job, job.id)
        row.input_checksum = checksum
    await job_repo.append_event(job.id, "stage_completed", {
        "from": "dispatched", "to": "verifying", "package_prefix": prefix,
    })
    directory = settings.data_dir / job.id.hex
    (directory / "package").mkdir(parents=True)
    (directory / "source.mp4").write_bytes(source_bytes)
    package = SimpleNamespace(duration_seconds=1.0, handoff={
        "source": {"sha256": checksum, "object_key": cloud.source_key(track_id)},
        "separation": {"sample_count": 44100},
    })
    settings.shizzle_pipeline = "cloud"
    settings.orchestrator_lease_seconds = 1.5
    orchestrator = Orchestrator(settings, worker_id="temporary")
    orchestrator.runpod = SimpleNamespace(
        poll=AsyncMock(return_value={"status": "COMPLETED", "id": job.runpod_job_id}),
        dispatch=AsyncMock(side_effect=AssertionError("Recovery must never dispatch")),
        cancel=AsyncMock(side_effect=AssertionError("Recovery must never cancel")),
    )
    renew = AsyncMock(wraps=orchestrator.jobs.renew_lease)
    monkeypatch.setattr(orchestrator.jobs, "renew_lease", renew)

    def construct(*, worker_id):
        orchestrator.worker_id = worker_id
        return orchestrator

    monkeypatch.setattr(command, "Orchestrator", construct)
    monkeypatch.setattr(command, "load_and_verify_package", lambda _path: package)
    monkeypatch.setattr(cloud, "load_and_verify_package", lambda _path: package)
    selected = []

    def download(_s3, _bucket, chosen_prefix, _destination):
        selected.append(chosen_prefix)
        assert chosen_prefix == prefix

    monkeypatch.setattr(cloud, "download_package", download)
    monkeypatch.setattr(cloud, "s3_client", lambda _settings: object())

    def transform(*_args):
        if outcome == "failure":
            raise IntakeError("Synthetic publication failure")
        return {"duration": 1.0, "title": "recovered"}

    monkeypatch.setattr(cloud, "transform", transform)
    monkeypatch.setattr(cloud, "stage", lambda *_args: "staged")

    class Publisher:
        def __init__(self, *_args):
            pass

        async def publish_async(self, *_args):
            # Outlive the initial lease: ready can commit only if the actual
            # orchestrator renewer keeps its ownership valid.
            await asyncio.sleep(2.2)
            if outcome == "interrupted":
                raise asyncio.CancelledError
            return SimpleNamespace(verification=None,
                                   already_published=outcome.startswith("existing"),
                                   s3_prefix=f"tracks/{track_id}/1",
                                   manifest_key=f"tracks/{track_id}/1/manifest.json")

    monkeypatch.setattr(cloud, "Publisher", Publisher)
    monkeypatch.setattr(cloud, "_read_json_or_none", lambda *_args: {
        "duration": 2.0 if outcome == "existing_different" else 1.0, "title": "recovered",
    })
    args = Namespace(job_id=str(job.id), provider_job_id=job.runpod_job_id,
                     input_checksum=checksum, recovery_id=str(uuid.uuid4()),
                     execute=True, use_completion_receipts=False)
    if outcome == "interrupted":
        with pytest.raises(asyncio.CancelledError):
            await command.recover(args)
    else:
        report = await command.recover(args)
        assert report["executed"] is True
        succeeded = outcome in {"success", "existing_same"}
        assert report["published"] is succeeded
        assert report["status"] == ("ready" if succeeded else "failed")
    current = await job_repo.get_job(job.id)
    assert current.runpod_job_id == job.runpod_job_id
    assert current.idempotency_key == job.idempotency_key
    assert current.lease_owner is None
    assert selected == [prefix]
    orchestrator.runpod.dispatch.assert_not_called()
    orchestrator.runpod.cancel.assert_not_called()
    if outcome != "failure":
        assert renew.await_count >= 1
    if outcome == "interrupted":
        assert current.status == JobStage.publishing
        claimed = await job_repo.claim_next(worker_id="replacement", lease_seconds=120)
        assert claimed.id == job.id
        assert claimed.status == JobStage.publishing


async def test_verification_failure_keeps_provenance_but_rejects_reused_id(job_repo, session_factory):
    job = await failed_publication(job_repo, session_factory)
    args = recovery_args(job)
    await job_repo.recover_publication(job.id, **args)
    await job_repo.fail_job(job.id, worker_id=args["worker_id"],
                            error_code=ErrorCode.CHECKSUM_MISMATCH,
                            error_detail="Transient verification failed with an exhausted attempt budget")
    # The old ownership credential must never be revived, even after failure.
    with pytest.raises(InvalidTransition):
        await job_repo.recover_publication(job.id, **args)
    replacement = {**args, "recovery_id": uuid.uuid4(), "worker_id": "new-recovery-owner"}
    recovered = await job_repo.recover_publication(job.id, **replacement)
    assert recovered.status == JobStage.verifying
    assert recovered.attempt == job.attempt
    assert recovered.runpod_job_id == job.runpod_job_id
    assert not await job_repo.renew_lease(job.id, worker_id=args["worker_id"], lease_seconds=120)

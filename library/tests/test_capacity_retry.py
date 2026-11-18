"""G3 capacity counting around retries: the reproduction from the verifier.

A confirmed attempt that failed leaves runpod_job_id/worker_phase='failed'
on the row; the retry's fresh reservation is CURRENT work (a submit timeout
leaves it outstanding, B3) and must consume capacity even though the row
still carries the closed execution's artifacts.
"""

from __future__ import annotations

import pytest_asyncio

from shizzle_server.db.models import JobStage, SourceType
from shizzle_server.db.repository import JobRepository
from shizzle_server.errors import ErrorCode


@pytest_asyncio.fixture
async def job_repo(session_factory) -> JobRepository:
    return JobRepository(session_factory)


async def _at_dispatched(job_repo, *, worker="w", title="T"):
    job = await job_repo.create_job(
        source_type=SourceType.upload, source_ref="source.mp4", title=title
    )
    assert (await job_repo.claim_next(worker_id=worker, lease_seconds=600)).id == job.id
    await job_repo.advance(
        job.id, worker_id=worker, from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await job_repo.advance(
        job.id, worker_id=worker, from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    return job


async def _retry_state(job_repo, job, *, worker="w"):
    """Attempt 0 confirmed then failed; retry scheduled; attempt 1 reserved.

    The reservation stays outstanding exactly as a submit timeout leaves it
    (B3: never redispatch, reconcile instead).
    """
    key0 = f"{job.id.hex}:0"
    assert await job_repo.reserve_dispatch(job.id, worker_id=worker, idempotency_key=key0)
    await job_repo.confirm_dispatch(job.id, idempotency_key=key0, runpod_job_id="rp-dead")
    await job_repo.record_worker_progress(job.id, phase="failed", worker_id=worker)
    await job_repo.schedule_retry(
        job.id,
        worker_id=worker,
        error_code=ErrorCode.RUNPOD_DISPATCH_FAILED,
        error_detail="worker died",
        retry_in_seconds=0.0,
    )
    retry = await job_repo.get_job(job.id)
    assert retry.attempt == 1
    key1 = f"{job.id.hex}:1"
    # The retry's claim re-arms the lease, then its reservation is outstanding.
    assert (await job_repo.claim_next(worker_id=worker, lease_seconds=600)).id == job.id
    assert await job_repo.reserve_dispatch(job.id, worker_id=worker, idempotency_key=key1)
    return key1


async def test_retry_reservation_counts_despite_closed_execution(job_repo):
    """Verifier reproduction: with cap 1, the retry-state job's reservation
    must consume the slot, so another job's reservation is refused."""
    first = await _at_dispatched(job_repo, title="First")
    await _retry_state(job_repo, first)
    # The closed execution alone would count zero; the retry reservation
    # must count one.
    assert await job_repo.count_runpod_dispatches() == 1

    second = await _at_dispatched(job_repo, worker="w2", title="Second")
    assert not await job_repo.reserve_dispatch(
        second.id, worker_id="w2", idempotency_key=f"{second.id.hex}:0", max_concurrent=1
    )


async def test_submit_timeout_reservation_counts_and_predecessor_does_not(job_repo):
    """Stepwise: the closed execution alone counts zero (B12); the moment the
    retry's reservation is outstanding (submit timeout shape), it counts."""
    job = await _at_dispatched(job_repo)
    key0 = f"{job.id.hex}:0"
    assert await job_repo.reserve_dispatch(job.id, worker_id="w", idempotency_key=key0)
    await job_repo.confirm_dispatch(job.id, idempotency_key=key0, runpod_job_id="rp-dead")
    await job_repo.record_worker_progress(job.id, phase="failed", worker_id="w")
    await job_repo.schedule_retry(
        job.id,
        worker_id="w",
        error_code=ErrorCode.RUNPOD_DISPATCH_FAILED,
        error_detail="worker died",
        retry_in_seconds=0.0,
    )
    assert await job_repo.count_runpod_dispatches() == 0  # closed, nothing live

    assert (await job_repo.claim_next(worker_id="w", lease_seconds=600)).id == job.id
    assert await job_repo.reserve_dispatch(
        job.id, worker_id="w", idempotency_key=f"{job.id.hex}:1"
    )
    assert await job_repo.count_runpod_dispatches() == 1  # the timeout-shaped slot

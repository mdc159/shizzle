"""Unit: state-machine transitions, retry accounting, structured error codes."""

from __future__ import annotations

import pytest

from shizzle_server.db.models import ALLOWED_TRANSITIONS, JobStage
from shizzle_server.db.repository import InvalidTransition
from shizzle_server.errors import ErrorCode, StageError


def test_transition_map_covers_all_stages():
    assert set(ALLOWED_TRANSITIONS) == set(JobStage)
    # Terminal stages go nowhere
    assert ALLOWED_TRANSITIONS[JobStage.ready] == frozenset()
    assert ALLOWED_TRANSITIONS[JobStage.failed] == frozenset()
    # The spec's happy path is representable
    path = [
        JobStage.pending,
        JobStage.downloading,
        JobStage.splitting,
        JobStage.verifying,
        JobStage.publishing,
    ]
    for a, b in zip(path, path[1:], strict=False):
        assert b in ALLOWED_TRANSITIONS[a]


async def test_advance_happy_path_records_timings(job_repo, upload_job):
    job = await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.pending,
        to_stage=JobStage.downloading,
        duration_s=0.5,
    )
    assert job.status == JobStage.downloading
    assert job.stage_timings == {"pending": 0.5}

    job = await job_repo.advance(
        upload_job.id,
        from_stage=JobStage.downloading,
        to_stage=JobStage.splitting,
        duration_s=1.25,
    )
    assert job.stage_timings == {"pending": 0.5, "downloading": 1.25}

    events = await job_repo.list_events(upload_job.id)
    completed = [e for e in events if e.event == "stage_completed"]
    assert len(completed) == 2
    assert completed[0].detail["from"] == "pending"
    assert completed[0].detail["to"] == "downloading"


async def test_illegal_transition_rejected(job_repo, upload_job):
    with pytest.raises(InvalidTransition):
        await job_repo.advance(
            upload_job.id, from_stage=JobStage.pending, to_stage=JobStage.ready
        )
    # Stale from_stage (double-advance) is also rejected
    await job_repo.advance(
        upload_job.id, from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    with pytest.raises(InvalidTransition):
        await job_repo.advance(
            upload_job.id, from_stage=JobStage.pending, to_stage=JobStage.downloading
        )
    # And the state was not corrupted
    job = await job_repo.get_job(upload_job.id)
    assert job.status == JobStage.downloading


async def test_schedule_retry_increments_attempt_and_sets_backoff(job_repo, upload_job):
    job = await job_repo.schedule_retry(
        upload_job.id,
        worker_id="w1",
        error_code=ErrorCode.DEMUCS_FAILED,
        error_detail="boom",
        retry_in_seconds=30,
    )
    assert job.attempt == 1
    assert job.error_code == "DEMUCS_FAILED"
    assert job.next_retry_at is not None
    assert job.lease_owner is None

    events = await job_repo.list_events(upload_job.id)
    retry = [e for e in events if e.event == "retry_scheduled"]
    assert len(retry) == 1
    assert retry[0].detail["attempt"] == 1
    assert retry[0].detail["error_code"] == "DEMUCS_FAILED"


async def test_fail_job_terminal_and_idempotent(job_repo, upload_job):
    job = await job_repo.fail_job(
        upload_job.id, error_code=ErrorCode.YTDLP_BLOCKED, error_detail="stub"
    )
    assert job.status == JobStage.failed
    assert job.error_code == "YTDLP_BLOCKED"
    # Duplicate failure report: no-op, still exactly one failed event
    await job_repo.fail_job(
        upload_job.id, error_code=ErrorCode.YTDLP_BLOCKED, error_detail="stub again"
    )
    events = await job_repo.list_events(upload_job.id)
    assert [e.event for e in events].count("failed") == 1
    job = await job_repo.get_job(upload_job.id)
    assert job.error_detail == "stub"  # first failure wins


def test_stage_error_carries_code_and_retryability():
    e = StageError(ErrorCode.S3_UPLOAD_FAILED, "network", retryable=True)
    assert e.code is ErrorCode.S3_UPLOAD_FAILED
    assert e.retryable is True
    stub = StageError(ErrorCode.YTDLP_BLOCKED, "phase 3", retryable=False)
    assert stub.retryable is False
    assert "YTDLP_BLOCKED" in str(stub)


def test_error_codes_are_stable_strings():
    # These names are persisted in the DB; renaming is a migration event.
    expected = {
        "YTDLP_BLOCKED",
        "DEMUCS_FAILED",
        "S3_UPLOAD_FAILED",
        "RUNPOD_TIMEOUT",
        "RUNPOD_DISPATCH_FAILED",
        "CHECKSUM_MISMATCH",
        "INTEGRITY_GATE_FAILED",
        "SOURCE_MISSING",
        "FFMPEG_FAILED",
        "PUBLISH_FAILED",
        "MAX_ATTEMPTS_EXCEEDED",
        "INTERNAL",
    }
    assert {c.value for c in ErrorCode} == expected

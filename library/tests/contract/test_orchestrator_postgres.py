"""Fault-injection contract tests on REAL Postgres (Mike's rule: fault
injection only where reality can't be provoked on demand — hard kills,
duplicate completions, and expired leases qualify).

Requires: docker compose -f deploy/vps/compose.yml --profile test -p shizzle-test up -d postgres
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, timedelta

import pytest
from sqlalchemy import text

from shizzle_server.db.models import JobStage, SourceType, utcnow
from shizzle_server.db.repository import (
    InvalidTransition,
    PublishRefusedError,
    track_id_for_job,
)
from shizzle_server.errors import ErrorCode

from .conftest import make_upload_job, wait_for

pytestmark = [pytest.mark.postgres, pytest.mark.integration]


def _effects_for(data_dir, job_hex: str) -> list[str]:
    effects = data_dir / job_hex / "effects.log"
    if not effects.exists():
        return []
    return [
        line.split()[1]
        for line in effects.read_text().splitlines()
        if line.startswith(job_hex)
    ]


async def _status(job_repo, job_id) -> JobStage:
    job = await job_repo.get_job(job_id)
    assert job is not None
    return job.status


class TestKillAndRestart:
    async def test_kill_mid_stage_then_restart_resumes_exactly_once(
        self, pg_repos, data_dir, spawn_orchestrator
    ):
        job_repo, track_repo, _ = pg_repos
        job = await make_upload_job(job_repo, data_dir)

        # Stage sleep 2 s: three split sub-stages take ~6 s. Kill mid-flight.
        orch_a = spawn_orchestrator("worker-a", shizzle_test_stage_sleep="2")

        async def first_effect_logged():
            return _effects_for(data_dir, job.id.hex).count("extracting") == 1

        await wait_for(first_effect_logged, timeout=30, desc="first sub-stage effect")
        await asyncio.sleep(0.5)  # now inside the second sub-stage's sleep
        orch_a.kill()  # hard kill — no cleanup, lease left dangling

        current = await job_repo.get_job(job.id)
        assert current.status == JobStage.splitting  # died mid-stage
        assert current.lease_owner == "worker-a"  # orphaned lease

        # Second instance: must wait out the expired lease, reclaim, resume.
        # Terminal state is FAILED at publishing: the C7 guard refuses the
        # test pipeline's local/ placeholder row.
        spawn_orchestrator("worker-b", shizzle_test_stage_sleep="2")
        settled = await wait_for(
            lambda: self._failed(job_repo, job.id),
            timeout=60,
            desc="job failed at publishing after restart",
        )
        assert settled.error_code == "PUBLISH_FAILED"

        # Idempotency proven by the effect counter: every sub-stage's effect
        # happened exactly once — completed work (marker present) was not
        # redone, interrupted work ran to completion exactly once.
        effects = _effects_for(data_dir, job.id.hex)
        for sub in ("extracting", "splitting", "encoding"):
            assert effects.count(sub) == 1, f"{sub} effects: {effects}"

        # No track row (C7); lease reclaim and the refusal are on the record.
        assert await track_repo.list_tracks() == []
        events = [e.event for e in await job_repo.list_events(job.id)]
        assert "lease_reclaimed" in events
        assert events.count("publish_refused") == 1
        assert "track_published" not in events
        assert events[0] == "created"  # unbroken history from birth

    @staticmethod
    async def _failed(job_repo, job_id):
        job = await job_repo.get_job(job_id)
        return job if job is not None and job.status == JobStage.failed else None


class TestLeaseExpiry:
    async def test_expired_foreign_lease_reclaimed_by_second_instance(
        self, pg_repos, pg_engine, data_dir, spawn_orchestrator
    ):
        """Simulate a dead orchestrator that left a lease behind (no process
        ever started for it), and verify a live instance takes the job over."""
        job_repo, track_repo, _ = pg_repos
        job = await make_upload_job(job_repo, data_dir)

        expired = utcnow() - timedelta(seconds=1)
        async with pg_engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE jobs SET lease_owner = 'dead-worker', "
                    "lease_expires_at = :exp WHERE id = :id"
                ),
                {"exp": expired, "id": job.id},
            )

        spawn_orchestrator("survivor")
        await wait_for(
            lambda: TestKillAndRestart._failed(job_repo, job.id),
            timeout=30,
            desc="survivor takes over the orphaned job (fails at publishing, C7)",
        )
        events = [e.event for e in await job_repo.list_events(job.id)]
        assert "lease_reclaimed" in events
        assert await track_repo.list_tracks() == []

    async def test_stale_worker_cannot_write_after_lease_reclaim(self, pg_repos, data_dir):
        """B2 fence on stage-outcome writes, the Postgres counterpart of the
        SQLite probe test: after the reclaim, the evicted worker's
        fail/retry/advance/publish raise InvalidTransition and its progress
        poll no-ops — the new owner's lease and the job state survive."""
        job_repo, _, _ = pg_repos
        job = await make_upload_job(job_repo, data_dir)
        t0 = utcnow()
        claimed = await job_repo.claim_next(worker_id="A", lease_seconds=5.0, now=t0)
        assert claimed is not None and claimed.id == job.id
        reclaimed = await job_repo.claim_next(
            worker_id="B", lease_seconds=300.0, now=t0 + timedelta(seconds=60)
        )
        assert reclaimed is not None and reclaimed.lease_owner == "B"

        with pytest.raises(InvalidTransition):
            await job_repo.fail_job(
                job.id,
                error_code=ErrorCode.INTERNAL,
                error_detail="stale worker failure",
                worker_id="A",
            )
        with pytest.raises(InvalidTransition):
            await job_repo.schedule_retry(
                job.id,
                worker_id="A",
                error_code=ErrorCode.DEMUCS_FAILED,
                error_detail="stale worker retry",
                retry_in_seconds=30,
            )
        with pytest.raises(InvalidTransition):
            await job_repo.advance(
                job.id,
                from_stage=JobStage.pending,
                to_stage=JobStage.downloading,
                worker_id="A",
            )
        survived = await job_repo.get_job(job.id)
        assert survived.status is JobStage.pending
        assert survived.lease_owner == "B"
        assert survived.attempt == 0

        # Walk B to publishing so the ownership fence — not the stage guard —
        # is what rejects A's publish.
        for a, b in [
            (JobStage.pending, JobStage.downloading),
            (JobStage.downloading, JobStage.splitting),
            (JobStage.splitting, JobStage.verifying),
            (JobStage.verifying, JobStage.publishing),
        ]:
            await job_repo.advance(job.id, from_stage=a, to_stage=b, worker_id="B")
        tid = track_id_for_job(job.id)
        with pytest.raises(InvalidTransition):
            await job_repo.publish_track(
                job.id,
                worker_id="A",
                title="Stale Publish",
                duration_seconds=1.0,
                s3_prefix=f"tracks/{tid}/1",
                manifest_key=f"tracks/{tid}/1/manifest.json",
            )
        survived = await job_repo.get_job(job.id)
        assert survived.status is JobStage.publishing
        assert survived.lease_owner == "B"

        # The C7 refusal path writes an event, so it is fenced the same way:
        # A's invalid-location publish raises instead of recording
        # publish_refused.
        with pytest.raises(InvalidTransition):
            await job_repo.publish_track(
                job.id,
                worker_id="A",
                title="Stale Phantom",
                duration_seconds=1.0,
                s3_prefix=f"local/{job.id.hex}",
                manifest_key=f"local/{job.id.hex}/stems.json",
            )
        assert not any(
            event.event == "publish_refused"
            for event in await job_repo.list_events(job.id)
        )
        survived = await job_repo.get_job(job.id)
        assert survived.status is JobStage.publishing
        assert survived.lease_owner == "B"

        assert (
            await job_repo.record_worker_progress(
                job.id, phase="downloading", worker_id="A"
            )
            is False
        )
        after = await job_repo.get_job(job.id)
        assert after.worker_phase is None
        assert after.worker_heartbeat_at is None

    async def test_live_lease_not_stolen(self, pg_repos, data_dir):
        """SKIP LOCKED + lease predicate: an unexpired lease blocks claims."""
        job_repo, _, _ = pg_repos
        job = await make_upload_job(job_repo, data_dir)
        claimed = await job_repo.claim_next(worker_id="holder", lease_seconds=60)
        assert claimed is not None and claimed.id == job.id
        assert await job_repo.claim_next(worker_id="thief", lease_seconds=60) is None


class TestDuplicateCompletion:
    async def test_concurrent_duplicate_completion_single_track(self, pg_repos, data_dir):
        """Two publishers racing the same completion: exactly one track row,
        deterministic id, second write converges (real Postgres unique pk)."""
        job_repo, track_repo, _ = pg_repos
        job = await make_upload_job(job_repo, data_dir)
        claimed = await job_repo.claim_next(worker_id="dup-publisher", lease_seconds=120)
        assert claimed is not None and claimed.id == job.id
        for a, b in [
            (JobStage.pending, JobStage.downloading),
            (JobStage.downloading, JobStage.splitting),
            (JobStage.splitting, JobStage.verifying),
            (JobStage.verifying, JobStage.publishing),
        ]:
            await job_repo.advance(job.id, from_stage=a, to_stage=b, worker_id="dup-publisher")

        kwargs = {
            "title": "Dup",
            "duration_seconds": 1.0,
            "s3_prefix": f"tracks/{track_id_for_job(job.id)}/1",
            "manifest_key": f"tracks/{track_id_for_job(job.id)}/1/manifest.json",
            "worker_id": "dup-publisher",
        }
        results = await asyncio.gather(
            job_repo.publish_track(job.id, **kwargs),
            job_repo.publish_track(job.id, **kwargs),
            return_exceptions=True,
        )
        tracks = [r for r in results if not isinstance(r, BaseException)]
        assert len(tracks) >= 1  # at least one side committed
        assert len({t.id for t in tracks}) == 1

        all_tracks = await track_repo.list_tracks()
        assert len(all_tracks) == 1
        final = await job_repo.get_job(job.id)
        assert final.status == JobStage.ready

        # And a straight sequential duplicate is a recorded no-op.
        again = await job_repo.publish_track(job.id, **kwargs)
        assert again.id == all_tracks[0].id
        events = [e.event for e in await job_repo.list_events(job.id)]
        assert events.count("track_published") == 1


class TestConcurrentRefusal:
    async def test_concurrent_refusals_record_one_event(self, pg_repos, data_dir):
        """Two publishers racing the same C7 refusal: publish_track locks the
        job row FOR UPDATE before the check-and-insert, so the second
        transaction observes the first's commit and exactly one
        publish_refused event lands (real Postgres row lock)."""
        job_repo, track_repo, _ = pg_repos
        job = await make_upload_job(job_repo, data_dir)
        claimed = await job_repo.claim_next(worker_id="refuser", lease_seconds=120)
        assert claimed is not None and claimed.id == job.id
        for a, b in [
            (JobStage.pending, JobStage.downloading),
            (JobStage.downloading, JobStage.splitting),
            (JobStage.splitting, JobStage.verifying),
            (JobStage.verifying, JobStage.publishing),
        ]:
            await job_repo.advance(job.id, from_stage=a, to_stage=b, worker_id="refuser")

        kwargs = {
            "title": "Phantom",
            "duration_seconds": 1.0,
            "s3_prefix": f"local/{job.id.hex}",
            "manifest_key": f"local/{job.id.hex}/stems.json",
            "worker_id": "refuser",
        }
        results = await asyncio.gather(
            job_repo.publish_track(job.id, **kwargs),
            job_repo.publish_track(job.id, **kwargs),
            return_exceptions=True,
        )
        assert len(results) == 2
        assert all(isinstance(r, PublishRefusedError) for r in results)
        events = [e.event for e in await job_repo.list_events(job.id)]
        assert events.count("publish_refused") == 1
        assert await track_repo.list_tracks() == []


class TestRetrySchedule:
    async def test_retry_backoff_honored_and_attempts_increment(
        self, pg_repos, data_dir, spawn_orchestrator
    ):
        job_repo, track_repo, _ = pg_repos
        job = await make_upload_job(job_repo, data_dir)

        # Two injected failures, then split succeeds. Base backoff 1 s ->
        # delays 1 s, 2 s. Publishing is then refused (C7): terminal failed.
        spawn_orchestrator("retrier", shizzle_test_fail_times="2")
        settled = await wait_for(
            lambda: TestKillAndRestart._failed(job_repo, job.id),
            timeout=45,
            desc="job retried, then failed at publishing",
        )
        assert settled.attempt == 2
        assert settled.error_code == "PUBLISH_FAILED"

        events = await job_repo.list_events(job.id)
        retries = [e for e in events if e.event == "retry_scheduled"]
        assert [e.detail["attempt"] for e in retries] == [1, 2]
        assert [e.detail["retry_in_seconds"] for e in retries] == [1.0, 2.0]
        assert all(e.detail["error_code"] == "DEMUCS_FAILED" for e in retries)

        # next_retry_at respected: the retry that followed each schedule
        # happened no earlier than the scheduled delay.
        completed = [
            e for e in events if e.event == "stage_completed" and e.detail["from"] == "splitting"
        ]
        assert len(completed) == 1
        gap = (completed[0].created_at - retries[1].created_at).total_seconds()
        assert gap >= 2.0, f"retry ran {gap:.2f}s after schedule; expected >= 2s backoff"

        assert await track_repo.list_tracks() == []


class TestConcurrentOrchestrators:
    async def test_two_live_instances_process_each_job_exactly_once(
        self, pg_repos, data_dir, spawn_orchestrator
    ):
        """FOR UPDATE SKIP LOCKED under real concurrency: no double execution."""
        job_repo, track_repo, _ = pg_repos
        jobs = [await make_upload_job(job_repo, data_dir) for _ in range(4)]

        spawn_orchestrator("peer-1", shizzle_test_stage_sleep="0.3")
        spawn_orchestrator("peer-2", shizzle_test_stage_sleep="0.3")

        async def all_settled():
            for j in jobs:
                job = await job_repo.get_job(j.id)
                if job.status != JobStage.failed:
                    return None
            return True

        await wait_for(all_settled, timeout=60, desc="all four jobs failed at publishing")

        # Every job ran exactly once and was refused exactly once (C7): no
        # track rows, one publish_refused per job.
        assert await track_repo.list_tracks() == []
        for j in jobs:
            effects = _effects_for(data_dir, j.id.hex)
            for sub in ("extracting", "splitting", "encoding"):
                assert effects.count(sub) == 1, f"job {j.id.hex}: {effects}"
            events = [e.event for e in await job_repo.list_events(j.id)]
            assert events.count("publish_refused") == 1
            assert "track_published" not in events


class TestUrlStub:
    async def test_url_job_fails_structured_on_postgres(self, pg_repos, spawn_orchestrator):
        job_repo, _, _ = pg_repos
        job = await job_repo.create_job(
            source_type=SourceType.url, source_ref="https://youtube.com/watch?v=zzz"
        )
        spawn_orchestrator("url-worker")

        async def failed():
            j = await job_repo.get_job(job.id)
            return j if j.status == JobStage.failed else None

        final = await wait_for(failed, timeout=30, desc="url job failed with stub code")
        assert final.error_code == "YTDLP_BLOCKED"
        assert final.attempt == 0


class TestSchemaContract:
    async def test_migration_produced_expected_tables_and_constraints(self, pg_engine):
        async with pg_engine.connect() as conn:
            tables = {
                r[0]
                for r in await conn.execute(
                    text(
                        "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
                    )
                )
            }
            assert {"jobs", "job_events", "tracks", "orchestrator_heartbeats"} <= tables
            assert "alembic_version" in tables

            # 0005_job_artist: jobs.artist is NOT NULL with a '' default so
            # pre-migration rows stay valid.
            columns = {
                r[0]: (r[1], r[2])
                for r in await conn.execute(
                    text(
                        "SELECT column_name, is_nullable, column_default "
                        "FROM information_schema.columns "
                        "WHERE table_name = 'jobs'"
                    )
                )
            }
            nullable, default = columns["artist"]
            assert nullable == "NO"
            assert default == "''::text"

            await conn.execute(
                text(
                    "INSERT INTO jobs (id, source_type, source_ref, status, "
                    "attempt, idempotency_key, profile_version, created_at, "
                    "updated_at) VALUES (:id, 'url', 'x', 'pending', 0, "
                    "'artist-default-probe', 1, now(), now())"
                ),
                {"id": uuid.uuid4()},
            )
            probed = await conn.execute(
                text("SELECT artist FROM jobs WHERE idempotency_key = 'artist-default-probe'")
            )
            assert probed.scalar_one() == ""

            # idempotency_key uniqueness is enforced at the DB, not just app code
            with pytest.raises(Exception, match="uq_jobs_idempotency_key"):
                for _i in range(2):
                    await conn.execute(
                        text(
                            "INSERT INTO jobs (id, source_type, source_ref, status, "
                            "attempt, idempotency_key, profile_version, created_at, "
                            "updated_at) VALUES (:id, 'url', 'x', 'pending', 0, "
                            "'same-key', 1, now(), now())"
                        ),
                        {"id": uuid.uuid4()},
                    )


class TestOrchestratorLiveness:
    async def test_heartbeat_row_updated(self, pg_repos, spawn_orchestrator):
        _, _, hb_repo = pg_repos
        spawn_orchestrator("hb-worker")

        async def beaten():
            return await hb_repo.latest()

        first = await wait_for(beaten, timeout=20, desc="first heartbeat")
        await asyncio.sleep(1.5)
        second = await hb_repo.latest()
        assert second > first


class TestDispatchedPark:
    """WS4 claim-based reconciliation on real Postgres: two workers, one
    dispatched job, exactly one claim per recheck interval, and park frees the
    lease without consuming an attempt.

    Repo-level (no subprocess): the SKIP LOCKED + lease + next_retry_at
    predicates are the contract the orchestrator loop relies on, and they are
    what makes ``handle_dispatched`` multi-worker safe by construction.
    """

    async def test_park_frees_lease_and_gates_reclaim_until_recheck(
        self, pg_repos, pg_engine, data_dir
    ):
        job_repo, _, _ = pg_repos
        job = await make_upload_job(job_repo, data_dir)
        claimed = await job_repo.claim_next(worker_id="A", lease_seconds=120)
        assert claimed is not None and claimed.id == job.id
        for a, b in [
            (JobStage.pending, JobStage.downloading),
            (JobStage.downloading, JobStage.dispatched),
        ]:
            await job_repo.advance(job.id, from_stage=a, to_stage=b, worker_id="A")

        # Worker A's live lease locks worker B out.
        assert await job_repo.claim_next(worker_id="B", lease_seconds=120) is None

        # A parks: lease freed, recheck scheduled, attempt untouched.
        await job_repo.park(job.id, worker_id="A", recheck_in_seconds=60.0)
        parked = await job_repo.get_job(job.id)
        assert parked.status == JobStage.dispatched
        assert parked.attempt == 0
        assert parked.lease_owner is None
        assert parked.lease_expires_at is None
        assert parked.next_retry_at is not None
        assert parked.next_retry_at.replace(tzinfo=UTC) > utcnow()

        # Advance past next_retry_at on the real DB, then B reclaims it.
        async with pg_engine.begin() as conn:
            await conn.execute(
                text("UPDATE jobs SET next_retry_at = :past WHERE id = :id"),
                {"past": utcnow() - timedelta(seconds=1), "id": job.id},
            )
        reclaimed = await job_repo.claim_next(worker_id="B", lease_seconds=120)
        assert reclaimed is not None and reclaimed.id == job.id
        assert reclaimed.lease_owner == "B"
        events = [e.event for e in await job_repo.list_events(job.id)]
        assert events.count("stage_completed") == 2  # -> downloading -> dispatched

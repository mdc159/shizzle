"""Contract: the admin assignment-release path under real Postgres locking.

The release endpoint's guarantee (one winner, node freed exactly once, one
audit event) depends on ``NodeRepository.expire``'s FOR UPDATE row lock and on
racing a node-side terminal report — behaviour SQLite cannot prove. Runs only
against the disposable compose test database (pytest.mark.postgres; skipped
cleanly when Postgres is unreachable).
"""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from shizzle_server.db import create_session_factory
from shizzle_server.db.models import Job, JobStage, NodeAssignmentKind, SourceType
from shizzle_server.db.repository import NodeRepository
from shizzle_server.main import create_app
from shizzle_server.settings import Settings

from .conftest import PG_URL

pytestmark = pytest.mark.postgres

SECRET = "contract-admin-secret"
HEADERS = {"x-shizzle-admin": SECRET}


@pytest_asyncio.fixture
async def release_env(pg_engine, data_dir):
    """The app plus one leaked open assignment on a terminal (failed) job."""
    sf = create_session_factory(pg_engine)
    nodes = NodeRepository(sf)
    await nodes.heartbeat("mac-mini", capabilities={"download": True, "separate": True})

    from shizzle_server.db.repository import JobRepository

    jobs = JobRepository(sf)
    job = await jobs.create_job(
        source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c", title="Leak"
    )
    claimed = await jobs.claim_next(worker_id="w", lease_seconds=60)
    assert claimed is not None and claimed.id == job.id
    assignment = await nodes.create_assignment(
        job.id,
        worker_id="w",
        node_id="mac-mini",
        kind=NodeAssignmentKind.separate,
        payload={},
        idempotency_key=f"{job.id.hex}:0",
    )
    assert assignment is not None
    # The job failed elsewhere; the assignment and the busy pointer leaked.
    async with sf() as session, session.begin():
        row = await session.get(Job, job.id)
        row.status = JobStage.failed
        row.error_code = "NODE_FAILED"

    settings = Settings(
        database_url=PG_URL,
        data_dir=data_dir,
        shizzle_embedded_orchestrator=False,
        shizzle_admin_proxy_secret=SECRET,
    )
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield sf, nodes, jobs, job, assignment, client


async def test_concurrent_releases_have_exactly_one_winner(release_env):
    sf, nodes, jobs, job, assignment, client = release_env
    responses = await asyncio.gather(
        client.post(f"/api/admin/assignments/{assignment.id}/release", headers=HEADERS),
        client.post(f"/api/admin/assignments/{assignment.id}/release", headers=HEADERS),
    )
    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200, 409]

    fresh = await nodes.get_assignment(assignment.id)
    assert fresh.status.value == "expired"
    assert "released by an administrator" in fresh.detail["expired_reason"]
    node = await nodes.get("mac-mini")
    assert node.busy_assignment_id is None

    events = await jobs.list_events(job.id)
    released = [e for e in events if e.event == "admin_assignment_released"]
    assert len(released) == 1


async def test_release_racing_a_node_report_leaves_one_terminal_state(release_env):
    sf, nodes, jobs, job, assignment, client = release_env
    release_task = client.post(f"/api/admin/assignments/{assignment.id}/release", headers=HEADERS)
    report_task = nodes.complete(assignment.id, node_id="mac-mini", detail={"late": True})
    release_response, reported = await asyncio.gather(release_task, report_task)

    assert release_response.status_code in (200, 409)
    fresh = await nodes.get_assignment(assignment.id)
    assert fresh.status.value in ("expired", "completed")  # exactly one writer won
    node = await nodes.get("mac-mini")
    assert node.busy_assignment_id is None  # freed either way

    # And a second release after either outcome is a clean 409.
    again = await client.post(f"/api/admin/assignments/{assignment.id}/release", headers=HEADERS)
    assert again.status_code == 409


async def test_runtime_settings_table_is_written_and_read_back(release_env, pg_engine):
    """The admin settings PUT path commits JSON the orchestrator's engine sees."""
    _, _, _, _, _, client = release_env
    put = await client.put(
        "/api/admin/settings",
        json={"nodeRouting": "download_only", "runpodMaxConcurrent": 2},
        headers=HEADERS,
    )
    assert put.status_code == 200
    body = put.json()
    assert body["nodeRouting"] == {"value": "download_only", "source": "database", "error": None}
    assert body["runpodMaxConcurrent"]["value"] == 2

    got = await client.get("/api/admin/settings", headers=HEADERS)
    assert got.json()["nodeRouting"]["value"] == "download_only"

    async with pg_engine.connect() as conn:
        rows = {
            r[0]: r[1]
            for r in await conn.execute(text("SELECT key, value FROM runtime_settings"))
        }
    assert rows["node_routing"] == '"download_only"' or rows["node_routing"] == "download_only"
    assert rows["runpod_max_concurrent"] in (2, "2")


async def test_concurrent_reservations_one_instance_takes_the_final_slot(
    pg_engine, data_dir
):
    """G3 atomicity on real Postgres: with a cap of 1 and no work dispatched,
    two orchestrator instances reserving different jobs concurrently must not
    both win the last slot. The capacity advisory xact lock serializes the
    check-and-reserve, so exactly one reservation commits."""
    from shizzle_server.db import create_engine, create_session_factory
    from shizzle_server.db.models import JobStage, SourceType
    from shizzle_server.db.repository import JobRepository

    async def make_repo():
        engine = create_engine(PG_URL)
        sf = create_session_factory(engine)
        return JobRepository(sf), engine

    repo_a, engine_a = await make_repo()
    repo_b, engine_b = await make_repo()
    try:
        jobs = []
        for worker in ("w1", "w2"):
            job = await repo_a.create_job(
                source_type=SourceType.upload, source_ref="source.mp4", title=worker
            )
            claimed = await repo_a.claim_next(worker_id=worker, lease_seconds=600)
            assert claimed is not None and claimed.id == job.id
            await repo_a.advance(
                job.id,
                worker_id=worker,
                from_stage=JobStage.pending,
                to_stage=JobStage.downloading,
            )
            await repo_a.advance(
                job.id,
                worker_id=worker,
                from_stage=JobStage.downloading,
                to_stage=JobStage.dispatched,
            )
            jobs.append(job)

        results = await asyncio.gather(
            repo_a.reserve_dispatch(
                jobs[0].id, worker_id="w1",
                idempotency_key=f"{jobs[0].id.hex}:0", max_concurrent=1,
            ),
            repo_b.reserve_dispatch(
                jobs[1].id, worker_id="w2",
                idempotency_key=f"{jobs[1].id.hex}:0", max_concurrent=1,
            ),
        )
        assert sorted(results) == [False, True]
        assert await repo_a.count_runpod_dispatches() == 1
        # The loser parked (no reservation of its own) — it may retry later.
        loser_events = await repo_a.list_events(jobs[results.index(False)].id)
        assert not any(e.event == "runpod_dispatch_started" for e in loser_events)
    finally:
        await engine_a.dispose()
        await engine_b.dispose()


async def test_concurrent_reservations_see_a_retry_reservation_slot(pg_engine):
    """G3 atomicity around retries: a job whose attempt 0 failed (row keeps
    runpod_job_id + worker_phase='failed') holds a real slot once its retry's
    reservation is outstanding (submit-timeout shape, B3). Two further jobs
    racing with cap 2 — one slot already consumed — must not both win."""
    from shizzle_server.db import create_engine, create_session_factory
    from shizzle_server.db.models import JobStage, SourceType
    from shizzle_server.db.repository import JobRepository
    from shizzle_server.errors import ErrorCode

    engine = create_engine(PG_URL)
    sf = create_session_factory(engine)
    repo = JobRepository(sf)
    try:
        retried = await repo.create_job(
            source_type=SourceType.upload, source_ref="source.mp4", title="retried"
        )
        assert (await repo.claim_next(worker_id="w0", lease_seconds=600)).id == retried.id
        for from_stage, to_stage in (
            (JobStage.pending, JobStage.downloading),
            (JobStage.downloading, JobStage.dispatched),
        ):
            await repo.advance(
                retried.id, worker_id="w0", from_stage=from_stage, to_stage=to_stage
            )
        key0 = f"{retried.id.hex}:0"
        assert await repo.reserve_dispatch(retried.id, worker_id="w0", idempotency_key=key0)
        await repo.confirm_dispatch(retried.id, idempotency_key=key0, runpod_job_id="rp-dead")
        await repo.record_worker_progress(retried.id, phase="failed", worker_id="w0")
        await repo.schedule_retry(
            retried.id,
            worker_id="w0",
            error_code=ErrorCode.RUNPOD_DISPATCH_FAILED,
            error_detail="worker died",
            retry_in_seconds=0.0,
        )
        assert (await repo.claim_next(worker_id="w0", lease_seconds=600)).id == retried.id
        assert await repo.reserve_dispatch(
            retried.id, worker_id="w0", idempotency_key=f"{retried.id.hex}:1"
        )
        assert await repo.count_runpod_dispatches() == 1

        repo_a = JobRepository(create_session_factory(create_engine(PG_URL)))
        repo_b = JobRepository(create_session_factory(create_engine(PG_URL)))
        competitors = []
        for worker in ("w1", "w2"):
            job = await repo.create_job(
                source_type=SourceType.upload, source_ref="source.mp4", title=worker
            )
            assert (await repo.claim_next(worker_id=worker, lease_seconds=600)).id == job.id
            for from_stage, to_stage in (
                (JobStage.pending, JobStage.downloading),
                (JobStage.downloading, JobStage.dispatched),
            ):
                await repo.advance(
                    job.id, worker_id=worker, from_stage=from_stage, to_stage=to_stage
                )
            competitors.append(job)

        results = await asyncio.gather(
            repo_a.reserve_dispatch(
                competitors[0].id, worker_id="w1",
                idempotency_key=f"{competitors[0].id.hex}:0", max_concurrent=2,
            ),
            repo_b.reserve_dispatch(
                competitors[1].id, worker_id="w2",
                idempotency_key=f"{competitors[1].id.hex}:0", max_concurrent=2,
            ),
        )
        # One slot of two is taken by the retry reservation: exactly one more.
        assert sorted(results) == [False, True]
        assert await repo.count_runpod_dispatches() == 2
    finally:
        await engine.dispose()

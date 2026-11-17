"""Runtime settings overrides (admin page): routing modes incl. download_only,
the RunPod concurrency limit, and the env fallback.

SQLite-backed and S3-free, like test_node_routing.py; the RunPod client is the
scripted FakeRunPodClient from test_orchestrator_unit. The overrides are
written through the same RuntimeSettingsRepository the admin API uses.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio

from shizzle_server.db.models import (
    JobStage,
    NodeAssignmentKind,
    RuntimeSetting,
    SourceType,
)
from shizzle_server.db.repository import RuntimeSettingsRepository
from shizzle_server.orchestrator.runtime import (
    NODE_ROUTING_KEY,
    RUNPOD_MAX_CONCURRENT_KEY,
    effective_node_routing,
    effective_runpod_max_concurrent,
    validate_setting,
)
from shizzle_server.settings import Settings
from test_node_routing import (
    MAC,
    _go_offline,
    _register,
    _run,
    _stop,
    _stub_package_ready,
    _stub_source_receipt,
    _wait_phase,
)
from test_orchestrator_unit import FakeRunPodClient, _stub_cloud_intake, wait_for_status


@pytest_asyncio.fixture
async def runtime_repo(session_factory) -> RuntimeSettingsRepository:
    return RuntimeSettingsRepository(session_factory)


@pytest.fixture
def cloud_settings(settings):
    """Same shape as test_node_routing.cloud_settings, defined locally so the
    two modules' fixtures never collide."""
    settings.shizzle_pipeline = "cloud"
    settings.runpod_api_key = "unit-test-key"
    settings.runpod_endpoint_id = "unit-test-endpoint"
    settings.runpod_poll_seconds = 0.02
    settings.node_poll_seconds = 0.05
    settings.node_online_seconds = 45.0
    settings.node_ack_seconds = 5.0
    settings.node_stall_seconds = 0.3
    settings.node_separate_wait_seconds = 0.3
    settings.node_download_wait_seconds = 0.5
    return settings


@pytest_asyncio.fixture
async def node_repo(session_factory):
    from shizzle_server.db.repository import NodeRepository

    return NodeRepository(session_factory)


def _ctx(settings: Settings, runtime: RuntimeSettingsRepository | None) -> SimpleNamespace:
    return SimpleNamespace(settings=settings, runtime=runtime)


# --- fallbacks and validation (unit level) --------------------------------------


async def test_missing_keys_fall_back_to_environment(settings, runtime_repo):
    """A missing runtime row falls back to the environment value — including
    non-null defaults (2) and the pause value (0), which previously vanished."""
    settings.shizzle_node_routing = "only"
    settings.runpod_max_concurrent = 2
    ctx = _ctx(settings, runtime_repo)
    assert await effective_node_routing(ctx) == "only"
    assert await effective_runpod_max_concurrent(ctx) == 2

    settings.runpod_max_concurrent = 0  # environment-level pause must hold
    assert await effective_runpod_max_concurrent(ctx) == 0

    settings.runpod_max_concurrent = None
    assert await effective_runpod_max_concurrent(ctx) is None


async def test_stored_null_cap_means_unlimited_not_env(settings, runtime_repo):
    """An explicitly stored JSON null is an override (no limit), distinct
    from a missing row (environment fallback)."""
    settings.runpod_max_concurrent = 2
    await runtime_repo.set_value(RUNPOD_MAX_CONCURRENT_KEY, None)
    ctx = _ctx(settings, runtime_repo)
    assert await effective_runpod_max_concurrent(ctx) is None


async def test_stored_values_override_environment(settings, runtime_repo):
    settings.shizzle_node_routing = "prefer"
    settings.runpod_max_concurrent = 5
    await runtime_repo.set_value(NODE_ROUTING_KEY, "download_only")
    await runtime_repo.set_value(RUNPOD_MAX_CONCURRENT_KEY, 1)
    ctx = _ctx(settings, runtime_repo)
    assert await effective_node_routing(ctx) == "download_only"
    assert await effective_runpod_max_concurrent(ctx) == 1


async def test_invalid_stored_values_fall_back_to_environment(settings, runtime_repo):
    await runtime_repo.set_value(NODE_ROUTING_KEY, "yolo")
    await runtime_repo.set_value(RUNPOD_MAX_CONCURRENT_KEY, -2)
    ctx = _ctx(settings, runtime_repo)
    assert await effective_node_routing(ctx) == settings.node_routing
    assert await effective_runpod_max_concurrent(ctx) == settings.runpod_max_concurrent


async def test_no_runtime_repo_falls_back_to_environment(settings):
    ctx = _ctx(settings, None)
    assert await effective_node_routing(ctx) == settings.node_routing
    assert await effective_runpod_max_concurrent(ctx) == settings.runpod_max_concurrent


def test_validate_setting_accepts_the_documented_domain():
    for mode in ("prefer", "only", "off", "download_only"):
        assert validate_setting(NODE_ROUTING_KEY, mode) is None
    assert validate_setting(NODE_ROUTING_KEY, "nonsense") is not None
    assert validate_setting(RUNPOD_MAX_CONCURRENT_KEY, 0) is None
    assert validate_setting(RUNPOD_MAX_CONCURRENT_KEY, 7) is None
    assert validate_setting(RUNPOD_MAX_CONCURRENT_KEY, None) is None
    assert validate_setting(RUNPOD_MAX_CONCURRENT_KEY, -1) is not None
    assert validate_setting("unknown_key", 1) is not None


# --- download_only: nodes download, RunPod separates -----------------------------


async def test_download_only_url_job_downloads_via_node_then_separates_on_runpod(
    cloud_settings, job_repo, node_repo, runtime_repo, monkeypatch
):
    """download_only keeps the node download path exactly as today, but
    separation never goes to a node: RunPod is dispatched at once."""
    _stub_cloud_intake(monkeypatch)
    receipt = _stub_source_receipt(monkeypatch, sha256="e" * 64, present=False)
    _stub_package_ready(monkeypatch, ready=False)
    await runtime_repo.set_value(NODE_ROUTING_KEY, "download_only")
    await _register(node_repo)
    job = await job_repo.create_job(
        source_type=SourceType.url, source_ref="https://youtu.be/dQw4w9WgXcQ", title="T"
    )
    fake = FakeRunPodClient([
        {"status": "IN_PROGRESS", "output": {"phase": "separating"}},
        {"status": "COMPLETED", "output": {"separation": {"sample_count": 44100}}},
    ])
    orch, task = await _run(cloud_settings, fake)

    async def scripted_node():
        dl = await _wait_download(node_repo, job.id)
        assert await node_repo.acknowledge(dl.id, node_id=MAC)
        receipt["present"] = True
        assert await node_repo.complete(dl.id, node_id=MAC, detail={"sha256": "e" * 64})
        return dl

    try:
        await asyncio.wait_for(scripted_node(), timeout=10)
        ready = await wait_for_status(job_repo, job.id, JobStage.ready, timeout=10)
    finally:
        await _stop(orch, task)

    # Separation went straight to RunPod: no separate assignment, no node wait.
    assert await node_repo.latest_for_job(job.id, kind=NodeAssignmentKind.separate) is None
    assert len(fake.dispatched) == 1
    assert ready.executor == "runpod"
    names = [e.event for e in await job_repo.list_events(job.id)]
    assert "runpod_dispatch_started" in names and "runpod_dispatched" in names
    assert "node_wait" not in names and "fallback_runpod" not in names


@pytest.mark.parametrize("routing", ["off", "download_only"])
async def test_routing_override_adopts_existing_separation_before_runpod(
    routing, cloud_settings, job_repo, node_repo, runtime_repo, upload_job
):
    """B16/G4: an override governs fresh choices, not crash recovery.

    Simulate a crash after the same-attempt assignment commit but before its
    reservation. The next pass must adopt that work even in RunPod-only modes.
    """
    from shizzle_server.orchestrator.stages import handle_dispatched

    await runtime_repo.set_value(NODE_ROUTING_KEY, routing)
    await _register(node_repo)
    claimed = await job_repo.claim_next(worker_id="w", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    await job_repo.advance(
        upload_job.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    claimed = await job_repo.advance(
        upload_job.id, worker_id="w", from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    dispatch_key = f"{upload_job.id.hex}:0"
    assignment = await node_repo.create_assignment(
        upload_job.id,
        worker_id="w",
        node_id=MAC,
        kind=NodeAssignmentKind.separate,
        payload={},
        idempotency_key=dispatch_key,
    )
    assert assignment is not None
    fake = FakeRunPodClient([])
    ctx = SimpleNamespace(
        job=claimed,
        settings=cloud_settings,
        jobs=job_repo,
        runpod=fake,
        worker_id="w",
        nodes=node_repo,
        runtime=runtime_repo,
        detail={},
        park_seconds=None,
    )

    assert await handle_dispatched(ctx) is None

    assert fake.dispatched == []
    assert ctx.park_seconds == cloud_settings.node_poll_seconds
    adopted = await job_repo.get_job(upload_job.id)
    assert adopted.runpod_job_id == f"node:{assignment.id}"
    assert "node_assignment_adopted" in [
        event.event for event in await job_repo.list_events(upload_job.id)
    ]


async def test_download_only_dispatches_runpod_with_no_node_wait(
    cloud_settings, job_repo, node_repo, runtime_repo, monkeypatch, upload_job
):
    """Even with a capable node merely offline (the case that earns the
    prefer-mode wait window), download_only dispatches RunPod at once."""
    _stub_cloud_intake(monkeypatch)
    await runtime_repo.set_value(NODE_ROUTING_KEY, "download_only")
    await _register(node_repo)
    await _go_offline(node_repo)
    fake = FakeRunPodClient([{"status": "IN_PROGRESS", "output": {"phase": "working"}}])
    orch, task = await _run(cloud_settings, fake)
    try:
        await wait_for_status(job_repo, upload_job.id, JobStage.dispatched, timeout=10)
        dispatched = await _wait_runpod_confirm(job_repo, upload_job.id)
    finally:
        await _stop(orch, task)

    assert len(fake.dispatched) == 1
    assert dispatched.runpod_job_id == fake.dispatched[0][0]
    events = [e.event for e in await job_repo.list_events(upload_job.id)]
    assert "node_wait" not in events
    assert fake.cancelled == []


# --- the RunPod concurrency limit -------------------------------------------------


class _GateRunPodClient(FakeRunPodClient):
    """Holds every job IN_PROGRESS until the test opens the gate.

    Keeps the single RunPod slot occupied deterministically while the second
    job's parked state is observed, instead of racing a scripted response
    list.
    """

    def __init__(self) -> None:
        super().__init__([])
        self.gate_open = False

    async def poll(self, runpod_job_id: str) -> dict:
        self.polled.append(runpod_job_id)
        if self.gate_open:
            return {"status": "COMPLETED", "output": {"separation": {"sample_count": 44100}}}
        return {"status": "IN_PROGRESS", "output": {"phase": "working"}}


async def test_runpod_limit_parks_without_attempt_and_never_disturbs_dispatched_work(
    cloud_settings, job_repo, runtime_repo, monkeypatch, upload_job
):
    """Limit 1: once one job holds RunPod, a second job parks (no attempt,
    one deduped runpod_wait per outage); when the first completes the second
    dispatches. Dispatched work is never cancelled."""
    _stub_cloud_intake(monkeypatch)
    await runtime_repo.set_value(RUNPOD_MAX_CONCURRENT_KEY, 1)
    fake = _GateRunPodClient()
    orch, task = await _run(cloud_settings, fake)
    try:
        # Job one takes the only RunPod slot first (no claim-order race: the
        # second job does not exist yet) and holds it (gate closed).
        first_dispatched = await _wait_runpod_confirm(job_repo, upload_job.id)
        assert len(fake.dispatched) == 1

        second = await job_repo.create_job(
            source_type=SourceType.upload, source_ref="source.mp4", title="Second"
        )
        second_dir = cloud_settings.data_dir / second.id.hex
        second_dir.mkdir(parents=True)
        (second_dir / "source.mp4").write_bytes(b"two")

        # The second job parks while the first holds the single RunPod slot.
        await wait_for_status(job_repo, second.id, JobStage.dispatched, timeout=10)
        await _wait_event_count(job_repo, second.id, "runpod_wait", exactly=1)
        # Give the parked pass a moment to re-check several times: the wait
        # event must stay deduped to one row per outage.
        await asyncio.sleep(0.3)
        waits = [
            e for e in await job_repo.list_events(second.id) if e.event == "runpod_wait"
        ]
        assert len(waits) == 1
        parked = await job_repo.get_job(second.id)
        assert parked.attempt == 0  # a park costs no attempt (B9 sibling)
        assert parked.runpod_job_id is None  # nothing dispatched for it yet
        assert "retry_scheduled" not in [
            e.event for e in await job_repo.list_events(second.id)
        ]

        # Open the gate: the first job completes, the slot frees, and only
        # then does the parked job dispatch.
        fake.gate_open = True
        await wait_for_status(job_repo, upload_job.id, JobStage.ready, timeout=10)
        second_ready = await wait_for_status(job_repo, second.id, JobStage.ready, timeout=15)
    finally:
        await _stop(orch, task)

    assert first_dispatched.runpod_job_id == fake.dispatched[0][0]
    assert second_ready.runpod_job_id == fake.dispatched[1][0]
    assert len(fake.dispatched) == 2
    assert fake.cancelled == []  # dispatched work is never disturbed


async def test_runpod_limit_zero_pauses_dispatch(
    cloud_settings, job_repo, runtime_repo, monkeypatch, upload_job
):
    _stub_cloud_intake(monkeypatch)
    await runtime_repo.set_value(RUNPOD_MAX_CONCURRENT_KEY, 0)
    fake = FakeRunPodClient([])
    orch, task = await _run(cloud_settings, fake)
    try:
        await wait_for_status(job_repo, upload_job.id, JobStage.dispatched, timeout=10)
        await _wait_event_count(job_repo, upload_job.id, "runpod_wait", exactly=1)
        await asyncio.sleep(0.3)
        parked = await job_repo.get_job(upload_job.id)
    finally:
        await _stop(orch, task)
    assert fake.dispatched == []
    assert parked.attempt == 0
    assert parked.runpod_job_id is None


async def test_runpod_limit_falls_back_to_env_when_cleared(
    cloud_settings, job_repo, runtime_repo, upload_job, monkeypatch, session_factory
):
    _stub_cloud_intake(monkeypatch)
    cloud_settings.runpod_max_concurrent = 1
    await runtime_repo.set_value(RUNPOD_MAX_CONCURRENT_KEY, 0)
    fake = FakeRunPodClient([
        {"status": "COMPLETED", "output": {"separation": {"sample_count": 44100}}}
    ])
    orch, task = await _run(cloud_settings, fake)
    try:
        # The DB override (0) pauses dispatch first...
        await wait_for_status(job_repo, upload_job.id, JobStage.dispatched, timeout=10)
        await _wait_event_count(job_repo, upload_job.id, "runpod_wait", exactly=1)
        # ...and deleting the key restores the environment value (limit 1).
        # JSON null is deliberately different: an explicit unlimited override.
        async with session_factory() as session, session.begin():
            row = await session.get(RuntimeSetting, RUNPOD_MAX_CONCURRENT_KEY)
            assert row is not None
            await session.delete(row)
        await _wait_runpod_confirm(job_repo, upload_job.id)
        await wait_for_status(job_repo, upload_job.id, JobStage.ready, timeout=10)
    finally:
        await _stop(orch, task)
    assert len(fake.dispatched) == 1


# --- runtime routing-mode overrides over the environment --------------------------


async def test_runtime_off_override_restores_ytdlp_blocked(
    cloud_settings, job_repo, node_repo, runtime_repo
):
    await runtime_repo.set_value(NODE_ROUTING_KEY, "off")
    await _register(node_repo)
    job = await job_repo.create_job(
        source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c"
    )
    orch, task = await _run(cloud_settings)
    try:
        failed = await wait_for_status(job_repo, job.id, JobStage.failed, timeout=10)
    finally:
        await _stop(orch, task)
    assert failed.error_code == "YTDLP_BLOCKED"
    assert await node_repo.latest_for_job(job.id) is None


async def test_runtime_only_override_waits_for_a_node_instead_of_runpod(
    cloud_settings, job_repo, node_repo, runtime_repo, monkeypatch, upload_job
):
    """Environment says prefer (immediate RunPod when no node is registered);
    the runtime override says only: never RunPod, wait for a node."""
    _stub_cloud_intake(monkeypatch)
    await runtime_repo.set_value(NODE_ROUTING_KEY, "only")
    fake = FakeRunPodClient([])
    orch, task = await _run(cloud_settings, fake)
    try:
        await wait_for_status(job_repo, upload_job.id, JobStage.dispatched, timeout=10)
        await _wait_phase(job_repo, upload_job.id, "waiting for a separation node")
        await asyncio.sleep(0.2)
        parked = await job_repo.get_job(upload_job.id)
    finally:
        await _stop(orch, task)
    assert fake.dispatched == []
    assert parked.runpod_job_id is None
    events = [e.event for e in await job_repo.list_events(upload_job.id)]
    assert events.count("node_wait") == 1


async def test_disabled_node_receives_no_new_assignment(
    cloud_settings, job_repo, node_repo, runtime_repo
):
    """A disabled node gets no new assignments; it simply is not a candidate."""
    await runtime_repo.set_value(NODE_ROUTING_KEY, "only")
    await _register(node_repo)
    assert await node_repo.set_disabled(MAC, True)
    job = await job_repo.create_job(
        source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c"
    )
    orch, task = await _run(cloud_settings)
    try:
        await wait_for_status(job_repo, job.id, JobStage.downloading, timeout=10)
        await _wait_phase(job_repo, job.id, "waiting for a download node")
        await asyncio.sleep(0.2)
    finally:
        await _stop(orch, task)
    assert await node_repo.latest_for_job(job.id) is None
    events = [e.event for e in await job_repo.list_events(job.id)]
    assert "node_assigned" not in events


# --- helpers ----------------------------------------------------------------------


async def _wait_download(node_repo, job_id):
    from test_node_routing import _wait_assignment

    return await _wait_assignment(node_repo, job_id, NodeAssignmentKind.download)


async def _wait_runpod_confirm(job_repo, job_id, timeout: float = 10.0):
    """Block until runpod_job_id is set on the job (dispatch confirmed)."""

    async def _poll():
        while True:
            job = await job_repo.get_job(job_id)
            if job is not None and job.runpod_job_id:
                return job
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_poll(), timeout=timeout)


async def _wait_event_count(job_repo, job_id, name: str, *, exactly: int, timeout: float = 10.0):
    async def _poll():
        while True:
            events = [
                e for e in await job_repo.list_events(job_id) if e.event == name
            ]
            if len(events) >= exactly:
                return events
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_poll(), timeout=timeout)


# --- count_runpod_dispatches semantics (G3) --------------------------------------


async def test_count_excludes_failed_worker_executions(job_repo, session_factory):
    """B12: a worker the orchestrator marked failed is treated as absent; its
    retry dispatches fresh under a new key. Counting the dead execution would
    make the retry count itself forever and park forever under any cap."""
    from datetime import timedelta

    from shizzle_server.db.models import Job, JobStage, utcnow

    job = await job_repo.create_job(source_type=SourceType.upload, source_ref="source.mp4")
    async with session_factory() as session, session.begin():
        row = await session.get(Job, job.id)
        row.status = JobStage.dispatched
        row.runpod_job_id = "rp-dead"
        row.executor = "runpod"
        row.worker_phase = "failed"
        row.worker_heartbeat_at = utcnow() - timedelta(hours=1)
    assert await job_repo.count_runpod_dispatches() == 0

    async with session_factory() as session, session.begin():
        row = await session.get(Job, job.id)
        row.worker_phase = "working"  # a live execution counts again
    assert await job_repo.count_runpod_dispatches() == 1


async def test_count_includes_unconfirmed_reservations_ignoring_download_executor(
    job_repo, node_repo, session_factory
):
    """A retained executor=node:<id> from an earlier URL download is NOT proof
    that a later separation reservation is node-bound. Only a separation
    assignment carrying the reservation's idempotency key is."""
    from shizzle_server.db.models import Job, JobStage, NodeAssignmentKind

    await node_repo.heartbeat("mac-mini", capabilities={"download": True, "separate": True})
    job = await job_repo.create_job(
        source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c"
    )
    key = f"{job.id.hex}:0"

    # URL download completed; executor stays node:mac-mini. The separation
    # reservation under this job's key is RunPod-bound (no assignment).
    async with session_factory() as session, session.begin():
        row = await session.get(Job, job.id)
        row.status = JobStage.dispatched
        row.executor = "node:mac-mini"
    claimed = await job_repo.claim_next(worker_id="w", lease_seconds=60)
    assert claimed.id == job.id
    assert await job_repo.reserve_dispatch(job.id, worker_id="w", idempotency_key=key)
    assert await job_repo.count_runpod_dispatches() == 1

    # The same reservation when a SEPARATION assignment carries its key is
    # node-bound capacity, not RunPod.
    assignment = await node_repo.create_assignment(
        job.id, worker_id="w", node_id="mac-mini",
        kind=NodeAssignmentKind.separate, payload={}, idempotency_key=key,
    )
    assert assignment is not None
    assert await job_repo.count_runpod_dispatches() == 0


async def test_capacity_count_uses_one_consistent_snapshot(
    job_repo, upload_job, session_factory, monkeypatch
):
    """One SQL statement gives one snapshot and avoids the old N+1 scan.

    The exact confirmation interleaving is covered against real Postgres in
    test_admin_postgres.py; SQLite test transactions cannot host that race.
    """
    from shizzle_server.db.repository import _count_runpod_work

    claimed = await job_repo.claim_next(worker_id="w", lease_seconds=60)
    assert claimed is not None and claimed.id == upload_job.id
    await job_repo.advance(
        upload_job.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await job_repo.advance(
        upload_job.id, worker_id="w", from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    key = f"{upload_job.id.hex}:0"
    assert await job_repo.reserve_dispatch(upload_job.id, worker_id="w", idempotency_key=key)

    async with session_factory() as session:
        original_execute = session.execute
        calls = 0

        async def count_statements(*args, **kwargs):
            nonlocal calls
            calls += 1
            return await original_execute(*args, **kwargs)

        monkeypatch.setattr(session, "execute", count_statements)
        assert await _count_runpod_work(session) == 1
        assert calls == 1


async def test_reserve_dispatch_enforces_the_cap_in_one_transaction(job_repo, upload_job):
    """The capacity gate lives inside reserve_dispatch: even without the
    caller's cheap pre-check, an at-cap reservation is refused atomically."""
    claimed = await job_repo.claim_next(worker_id="w", lease_seconds=60)
    assert claimed.id == upload_job.id
    await job_repo.advance(
        upload_job.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await job_repo.advance(
        upload_job.id, worker_id="w", from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    key = f"{upload_job.id.hex}:0"
    assert not await job_repo.reserve_dispatch(
        upload_job.id, worker_id="w", idempotency_key=key, max_concurrent=0
    )  # cap 0 refuses even the first reservation
    assert await job_repo.reserve_dispatch(
        upload_job.id, worker_id="w", idempotency_key=key
    )  # without a cap the reservation lands
    assert not await job_repo.reserve_dispatch(
        upload_job.id, worker_id="w", idempotency_key=key, max_concurrent=1
    )  # the outstanding reservation blocks, independent of the cap


async def test_concurrent_first_setting_writes_use_an_atomic_upsert(session_factory):
    from shizzle_server.db.repository import RuntimeSettingsRepository

    first = RuntimeSettingsRepository(session_factory)
    second = RuntimeSettingsRepository(session_factory)
    await asyncio.gather(
        first.set_value(RUNPOD_MAX_CONCURRENT_KEY, 1),
        second.set_value(RUNPOD_MAX_CONCURRENT_KEY, 2),
    )
    rows = await first.get_all()
    assert set(rows) == {RUNPOD_MAX_CONCURRENT_KEY}
    assert rows[RUNPOD_MAX_CONCURRENT_KEY].value in (1, 2)


def test_runpod_max_concurrent_env_binding(monkeypatch):
    """The documented environment knob actually binds (RUNPOD_MAX_CONCURRENT,
    no SHIZZLE_ prefix — like the other runpod_* knobs)."""
    from shizzle_server.settings import Settings

    monkeypatch.delenv("RUNPOD_MAX_CONCURRENT", raising=False)
    assert Settings().runpod_max_concurrent is None
    monkeypatch.setenv("RUNPOD_MAX_CONCURRENT", "3")
    assert Settings().runpod_max_concurrent == 3
    monkeypatch.setenv("RUNPOD_MAX_CONCURRENT", "0")
    assert Settings().runpod_max_concurrent == 0
    monkeypatch.setenv("RUNPOD_MAX_CONCURRENT", "-1")
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        Settings()

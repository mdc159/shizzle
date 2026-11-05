"""Unit: orchestrator routing through worker nodes (docs/WORKER-NODES.md).

SQLite-backed, S3-free: the source receipt and handoff checks are stubbed at
the module boundary (``nodes._read_source_receipt`` / ``_verify_source_receipt``
and ``cloud.package_ready``), and the cloud intake is stubbed exactly as the
RunPod end-to-end tests stub it. A scripted "node" drives assignments through
the same repository the API uses.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
import pytest_asyncio

from shizzle_server.db.models import (
    JobStage,
    Node,
    NodeAssignmentKind,
    NodeAssignmentStatus,
    SourceType,
    utcnow,
)
from shizzle_server.db.repository import NodeRepository, track_id_for_job
from shizzle_server.orchestrator import cloud, nodes
from shizzle_server.orchestrator.loop import Orchestrator
from test_orchestrator_unit import FakeRunPodClient, _stub_cloud_intake, wait_for_status

MAC = "mac-mini"


@pytest.fixture
def cloud_settings(settings):
    settings.shizzle_pipeline = "cloud"
    settings.runpod_api_key = "unit-test-key"
    settings.runpod_endpoint_id = "unit-test-endpoint"
    settings.runpod_poll_seconds = 0.02
    settings.node_poll_seconds = 0.05
    settings.node_online_seconds = 45.0
    settings.node_ack_seconds = 0.3
    settings.node_stall_seconds = 0.3
    settings.node_separate_wait_seconds = 0.3
    settings.node_download_wait_seconds = 0.5
    return settings


@pytest_asyncio.fixture
async def node_repo(session_factory) -> NodeRepository:
    return NodeRepository(session_factory)


async def _register(node_repo: NodeRepository, node_id: str = MAC, **caps) -> None:
    await node_repo.heartbeat(
        node_id,
        display_name=node_id,
        capabilities={"download": True, "separate": True, "device": "mps", **caps},
        agent_version="test",
    )


async def _go_offline(node_repo: NodeRepository, node_id: str = MAC) -> None:
    async with node_repo._sf() as session, session.begin():
        node = await session.get(Node, node_id)
        node.last_seen_at = utcnow() - timedelta(hours=1)


async def _run(settings, fake: FakeRunPodClient | None = None):
    orch = Orchestrator(settings, worker_id="node-unit")
    orch.runpod = fake or FakeRunPodClient([])
    task = asyncio.create_task(orch.run_forever())
    return orch, task


async def _stop(orch, task):
    orch.request_stop()
    await asyncio.wait_for(task, timeout=10)


async def _wait_assignment(node_repo, job_id, kind, *, status=None, after=None, timeout=5.0):
    """The latest assignment of ``kind`` for the job once it exists (optionally in ``status``)."""

    async def _poll():
        while True:
            current = await node_repo.latest_for_job(job_id, kind=kind)
            if (
                current is not None
                and (status is None or current.status == status)
                and (after is None or current.id != after)
            ):
                return current
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(_poll(), timeout=timeout)


async def _wait_phase(job_repo, job_id, phase: str, timeout: float = 5.0) -> None:
    """Block until the orchestrator has mirrored ``phase`` onto the job."""

    async def _poll():
        while (await job_repo.get_job(job_id)).worker_phase != phase:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_poll(), timeout=timeout)


def _stub_source_receipt(monkeypatch, *, sha256: str = "c" * 64, problem: str | None = None,
                         present: bool = True) -> dict:
    """Stub the S3 receipt; returns a state dict whose ``present`` flag tests can flip."""
    state = {"present": present}

    async def _read(ctx, track_id):
        if not state["present"]:
            return None
        return {
            "interface": nodes.SOURCE_INTERFACE,
            "object_key": cloud.source_key(track_id),
            "track_id": str(track_id),
            "sha256": sha256,
            "bytes": 1234,
            "duration_seconds": 42.0,
            "source_ref": "youtube-dQw4w9WgXcQ",
        }

    async def _verify(ctx, track_id, receipt):
        return problem

    monkeypatch.setattr(nodes, "_read_source_receipt", _read)
    monkeypatch.setattr(nodes, "_verify_source_receipt", _verify)
    return state


def _stub_package_ready(monkeypatch, ready: bool = True):
    async def _ready(ctx, *, idempotency_key):
        return ready

    monkeypatch.setattr(cloud, "package_ready", _ready)


# --- identity helpers -----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "video_id"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1&t=30s", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://music.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/watch?v=short", None),
        ("https://vimeo.com/12345", None),
        ("https://user:pw@www.youtube.com/watch?v=dQw4w9WgXcQ", None),
        ("not a url", None),
    ],
)
def test_youtube_video_id(url, video_id):
    assert nodes.youtube_video_id(url) == video_id


def test_source_ref_is_stable_and_namespaced():
    assert nodes.source_ref_for_url("https://youtu.be/dQw4w9WgXcQ") == "youtube-dQw4w9WgXcQ"
    other = nodes.source_ref_for_url("https://vimeo.com/12345")
    assert other.startswith("sha256-") and len(other) == len("sha256-") + 32
    assert other == nodes.source_ref_for_url("  https://vimeo.com/12345 ")


# --- downloading: URL jobs ----------------------------------------------------


async def test_url_job_download_is_assigned_to_an_online_node(
    cloud_settings, job_repo, node_repo
):
    await _register(node_repo)
    job = await job_repo.create_job(
        source_type=SourceType.url,
        source_ref="https://youtu.be/dQw4w9WgXcQ",
        title="Never Gonna",
        artist="Rick",
    )
    orch, task = await _run(cloud_settings)
    try:
        assignment = await _wait_assignment(node_repo, job.id, NodeAssignmentKind.download)
    finally:
        await _stop(orch, task)

    assert assignment.node_id == MAC
    assert assignment.status == NodeAssignmentStatus.assigned
    track_id = track_id_for_job(job.id)
    assert assignment.payload == {
        "attempt": 0,
        "url": "https://youtu.be/dQw4w9WgXcQ",
        "source_ref": "youtube-dQw4w9WgXcQ",
        "track_id": str(track_id),
        "bucket": cloud_settings.s3_media_bucket,
        "source_key": f"sources/{track_id}/source.mp4",
        "source_json_key": f"sources/{track_id}/source.json",
        "max_duration_seconds": cloud_settings.max_duration_seconds,
        "max_bytes": cloud_settings.max_upload_bytes,
        "title": "Never Gonna",
        "artist": "Rick",
    }
    current = await job_repo.get_job(job.id)
    assert current.status == JobStage.downloading
    assert current.executor == f"node:{MAC}"
    assert current.worker_phase == f"{MAC}: assigned"
    assert (await node_repo.get(MAC)).busy_assignment_id == assignment.id
    events = [e.event for e in await job_repo.list_events(job.id)]
    assert "node_assigned" in events and "node_wait" not in events


async def test_url_job_runs_download_then_separation_on_the_node_to_ready(
    cloud_settings, job_repo, track_repo, node_repo, monkeypatch
):
    """The whole node path: download assignment -> source receipt proven ->
    separation assignment under the dispatch key -> handoff proven ->
    verifying/publishing -> ready. RunPod is never called."""
    _stub_cloud_intake(monkeypatch)
    receipt = _stub_source_receipt(monkeypatch, sha256="d" * 64, present=False)
    package = {"ready": False}

    async def _ready(ctx, *, idempotency_key):
        return package["ready"]

    monkeypatch.setattr(cloud, "package_ready", _ready)
    await _register(node_repo)
    job = await job_repo.create_job(
        source_type=SourceType.url, source_ref="https://youtu.be/dQw4w9WgXcQ", title="T"
    )
    fake = FakeRunPodClient([])
    orch, task = await _run(cloud_settings, fake)

    async def scripted_node():
        dl = await _wait_assignment(node_repo, job.id, NodeAssignmentKind.download)
        assert await node_repo.acknowledge(dl.id, node_id=MAC)
        assert await node_repo.progress(dl.id, node_id=MAC, phase="download: 50%")
        await _wait_phase(job_repo, job.id, f"{MAC}: download: 50%")
        receipt["present"] = True  # source.json written last, then the report
        assert await node_repo.complete(dl.id, node_id=MAC, detail={"sha256": "d" * 64})
        sep = await _wait_assignment(node_repo, job.id, NodeAssignmentKind.separate)
        assert sep.idempotency_key == f"{job.id.hex}:0"
        assert sep.payload["input_key"] == cloud.source_key(track_id_for_job(job.id))
        assert sep.payload["idempotency_key"] == sep.idempotency_key
        assert await node_repo.acknowledge(sep.id, node_id=MAC)
        assert await node_repo.progress(sep.id, node_id=MAC, phase="separate: 80%")
        await _wait_phase(job_repo, job.id, f"{MAC}: separate: 80%")
        package["ready"] = True  # handoff.json written last, then the report
        assert await node_repo.complete(sep.id, node_id=MAC, detail={"uploads": 7})
        return dl, sep

    try:
        dl, sep = await asyncio.wait_for(scripted_node(), timeout=10)
        ready = await wait_for_status(job_repo, job.id, JobStage.ready, timeout=10)
    finally:
        await _stop(orch, task)

    assert fake.dispatched == []
    assert ready.input_checksum == "d" * 64
    assert ready.executor == f"node:{MAC}"
    assert ready.runpod_job_id == nodes.executor_id(sep.id)
    assert ready.track_id == track_id_for_job(job.id)
    assert (await track_repo.get(ready.track_id)) is not None
    events = await job_repo.list_events(job.id)
    names = [e.event for e in events]
    assert names.count("node_assigned") == 2
    assert "node_completed" in names
    assert "runpod_dispatch_started" in names and "runpod_dispatched" in names
    assert "worker_completed" in names
    assert "fallback_runpod" not in names and "node_wait" not in names
    confirmed = next(e for e in events if e.event == "runpod_dispatched")
    assert confirmed.detail["runpod_job_id"] == nodes.executor_id(sep.id)
    completed = next(e for e in events if e.event == "stage_completed"
                     and e.detail.get("from") == "dispatched")
    assert completed.detail["package_prefix"] == cloud.attempt_prefix(
        track_id_for_job(job.id), f"{job.id.hex}:0"
    )
    # Progress mirrored the node's phases onto the job.
    phases = [e.detail["phase"] for e in events if e.event == "worker_progress"]
    assert f"{MAC}: download: 50%" in phases and f"{MAC}: separate: 80%" in phases
    assert (await node_repo.get(MAC)).busy_assignment_id is None


async def test_url_job_waits_then_fails_without_any_download_node(
    cloud_settings, job_repo
):
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c")
    orch, task = await _run(cloud_settings)
    try:
        failed = await wait_for_status(job_repo, job.id, JobStage.failed, timeout=10)
    finally:
        await _stop(orch, task)
    assert failed.error_code == "NO_NODE_AVAILABLE"
    assert failed.attempt == 0  # non-retryable: no attempt burned waiting
    events = [e.event for e in await job_repo.list_events(job.id)]
    assert events.count("node_wait") == 1  # one row per outage, not per poll
    assert "retry_scheduled" not in events


async def test_routing_off_keeps_the_ytdlp_blocked_contract(cloud_settings, job_repo, node_repo):
    cloud_settings.shizzle_node_routing = "off"
    await _register(node_repo)
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c")
    orch, task = await _run(cloud_settings)
    try:
        failed = await wait_for_status(job_repo, job.id, JobStage.failed, timeout=10)
    finally:
        await _stop(orch, task)
    assert failed.error_code == "YTDLP_BLOCKED"
    assert await node_repo.latest_for_job(job.id) is None


async def test_unacknowledged_download_expires_and_is_reassigned(
    cloud_settings, job_repo, node_repo
):
    await _register(node_repo)
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c")
    orch, task = await _run(cloud_settings)
    try:
        first = await _wait_assignment(node_repo, job.id, NodeAssignmentKind.download)
        second = await _wait_assignment(
            node_repo, job.id, NodeAssignmentKind.download, after=first.id, timeout=10
        )
    finally:
        await _stop(orch, task)
    expired = await node_repo.get_assignment(first.id)
    assert expired.status == NodeAssignmentStatus.expired
    assert "not acknowledged" in expired.detail["expired_reason"]
    assert second.status in (NodeAssignmentStatus.assigned, NodeAssignmentStatus.acknowledged)
    current = await job_repo.get_job(job.id)
    assert current.status == JobStage.downloading and current.attempt >= 1
    events = await job_repo.list_events(job.id)
    retry = next(e for e in events if e.event == "retry_scheduled")
    assert retry.detail["error_code"] == "NODE_FAILED"
    assert any(e.event == "node_expired" for e in events)


async def test_node_download_failure_schedules_a_retry(cloud_settings, job_repo, node_repo):
    await _register(node_repo)
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c")
    orch, task = await _run(cloud_settings)
    try:
        first = await _wait_assignment(node_repo, job.id, NodeAssignmentKind.download)
        assert await node_repo.fail(
            first.id, node_id=MAC, detail={"code": "YOUTUBE_SIGN_IN_REQUIRED", "detail": "bot check"}
        )
        second = await _wait_assignment(
            node_repo, job.id, NodeAssignmentKind.download, after=first.id, timeout=10
        )
    finally:
        await _stop(orch, task)
    assert second.id != first.id
    events = await job_repo.list_events(job.id)
    failed = next(e for e in events if e.event == "node_failed")
    assert failed.detail["code"] == "YOUTUBE_SIGN_IN_REQUIRED"
    retry = next(e for e in events if e.event == "retry_scheduled")
    assert retry.detail["error_code"] == "NODE_FAILED"
    assert "YOUTUBE_SIGN_IN_REQUIRED" in retry.detail["error_detail"]


async def test_rejected_source_receipt_is_a_retryable_checksum_failure(
    cloud_settings, job_repo, node_repo, monkeypatch
):
    _stub_source_receipt(monkeypatch, problem="source.mp4 is 9 bytes, receipt says 1234")
    await _register(node_repo)
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c")
    orch, task = await _run(cloud_settings)
    try:
        first = await _wait_assignment(node_repo, job.id, NodeAssignmentKind.download)
        assert await node_repo.complete(first.id, node_id=MAC, detail={})

        async def _retried():
            while True:
                events = await job_repo.list_events(job.id)
                if any(e.event == "retry_scheduled" for e in events):
                    return events
                await asyncio.sleep(0.02)

        events = await asyncio.wait_for(_retried(), timeout=10)
    finally:
        await _stop(orch, task)
    retry = next(e for e in events if e.event == "retry_scheduled")
    assert retry.detail["error_code"] == "CHECKSUM_MISMATCH"
    assert "receipt rejected" in retry.detail["error_detail"]
    assert (await job_repo.get_job(job.id)).input_checksum is None


# --- dispatched: separation routing ------------------------------------------


async def test_upload_job_prefers_an_online_separation_node(
    cloud_settings, job_repo, track_repo, node_repo, upload_job, monkeypatch
):
    _stub_cloud_intake(monkeypatch)
    _stub_package_ready(monkeypatch, True)
    await _register(node_repo)
    fake = FakeRunPodClient([])
    orch, task = await _run(cloud_settings, fake)
    try:
        sep = await _wait_assignment(node_repo, upload_job.id, NodeAssignmentKind.separate)
        assert sep.payload["track_id"] == str(track_id_for_job(upload_job.id))
        assert await node_repo.complete(sep.id, node_id=MAC, detail={})
        ready = await wait_for_status(job_repo, upload_job.id, JobStage.ready, timeout=10)
    finally:
        await _stop(orch, task)
    assert fake.dispatched == []
    assert ready.executor == f"node:{MAC}"
    assert ready.runpod_job_id == nodes.executor_id(sep.id)


async def test_separation_falls_back_to_runpod_after_the_wait_window(
    cloud_settings, job_repo, node_repo, upload_job, monkeypatch
):
    """A registered node that is offline earns the wait window; then RunPod (B17)."""
    _stub_cloud_intake(monkeypatch)
    await _register(node_repo)
    await _go_offline(node_repo)
    fake = FakeRunPodClient([
        {"status": "COMPLETED", "output": {"separation": {"sample_count": 44100}}},
    ])
    orch, task = await _run(cloud_settings, fake)
    try:
        ready = await wait_for_status(job_repo, upload_job.id, JobStage.ready, timeout=10)
    finally:
        await _stop(orch, task)
    assert len(fake.dispatched) == 1
    assert fake.dispatched[0][1] == f"{upload_job.id.hex}:0"
    assert ready.runpod_job_id == "rp-1"
    assert ready.executor is None  # RunPod attempts carry no node executor
    events = await job_repo.list_events(upload_job.id)
    names = [e.event for e in events]
    assert names.index("node_wait") < names.index("fallback_runpod") < names.index(
        "runpod_dispatch_started"
    )
    fallback = next(e for e in events if e.event == "fallback_runpod")
    assert fallback.detail["idempotency_key"] == f"{upload_job.id.hex}:0"
    assert fallback.detail["waited_seconds"] >= cloud_settings.node_separate_wait_seconds
    assert await node_repo.latest_for_job(upload_job.id) is None


async def test_no_registered_node_dispatches_to_runpod_immediately(
    cloud_settings, job_repo, upload_job, monkeypatch
):
    _stub_cloud_intake(monkeypatch)
    fake = FakeRunPodClient([
        {"status": "COMPLETED", "output": {"separation": {"sample_count": 44100}}},
    ])
    orch, task = await _run(cloud_settings, fake)
    try:
        await wait_for_status(job_repo, upload_job.id, JobStage.ready, timeout=10)
    finally:
        await _stop(orch, task)
    assert len(fake.dispatched) == 1
    names = [e.event for e in await job_repo.list_events(upload_job.id)]
    assert "node_wait" not in names and "fallback_runpod" not in names


async def test_routing_only_never_dispatches_to_runpod(
    cloud_settings, job_repo, node_repo, upload_job, monkeypatch
):
    _stub_cloud_intake(monkeypatch)
    cloud_settings.shizzle_node_routing = "only"
    await _register(node_repo)
    await _go_offline(node_repo)
    fake = FakeRunPodClient([])
    orch, task = await _run(cloud_settings, fake)
    try:
        await asyncio.sleep(1.0)  # well past node_separate_wait_seconds
    finally:
        await _stop(orch, task)
    assert fake.dispatched == []
    current = await job_repo.get_job(upload_job.id)
    assert current.status == JobStage.dispatched
    assert current.worker_phase == "waiting for a separation node"
    names = [e.event for e in await job_repo.list_events(upload_job.id)]
    assert names.count("node_wait") == 1 and "fallback_runpod" not in names


async def test_stalled_node_separation_is_expired_and_redispatched_to_another_node(
    cloud_settings, job_repo, node_repo, upload_job, monkeypatch
):
    _stub_cloud_intake(monkeypatch)
    _stub_package_ready(monkeypatch, False)
    await _register(node_repo)
    await _register(node_repo, "dell-9530", device="cuda")
    fake = FakeRunPodClient([])
    orch, task = await _run(cloud_settings, fake)
    try:
        first = await _wait_assignment(node_repo, upload_job.id, NodeAssignmentKind.separate)
        assert await node_repo.acknowledge(first.id, node_id=first.node_id)
        # ...then silence past node_stall_seconds: expired, attempt 1 under a new
        # key, and the node that stalled is not picked again for this job.
        second = await _wait_assignment(
            node_repo, upload_job.id, NodeAssignmentKind.separate, after=first.id, timeout=10
        )
        assert second.idempotency_key == f"{upload_job.id.hex}:1"
        assert second.node_id != first.node_id
        _stub_package_ready(monkeypatch, True)
        assert await node_repo.complete(second.id, node_id=second.node_id, detail={})
        ready = await wait_for_status(job_repo, upload_job.id, JobStage.ready, timeout=10)
    finally:
        await _stop(orch, task)
    assert fake.dispatched == []
    assert (await node_repo.get_assignment(first.id)).status == NodeAssignmentStatus.expired
    assert ready.runpod_job_id == nodes.executor_id(second.id)
    events = await job_repo.list_events(upload_job.id)
    assert [e.detail["error_code"] for e in events if e.event == "retry_scheduled"] == ["NODE_FAILED"]
    assert any(e.event == "node_expired" for e in events)


async def test_only_node_that_stalled_this_job_yields_to_runpod(
    cloud_settings, job_repo, node_repo, upload_job, monkeypatch
):
    """A node that keeps heartbeating but never acknowledges must not block the
    RunPod fallback forever: after it expires, it is not picked again for this
    job, so the wait window runs and RunPod takes the retry (B17)."""
    _stub_cloud_intake(monkeypatch)
    _stub_package_ready(monkeypatch, False)
    await _register(node_repo)
    fake = FakeRunPodClient([
        {"status": "COMPLETED", "output": {"separation": {"sample_count": 44100}}},
    ])
    orch, task = await _run(cloud_settings, fake)
    try:
        first = await _wait_assignment(node_repo, upload_job.id, NodeAssignmentKind.separate)
        ready = await wait_for_status(job_repo, upload_job.id, JobStage.ready, timeout=15)
    finally:
        await _stop(orch, task)
    assert (await node_repo.get_assignment(first.id)).status == NodeAssignmentStatus.expired
    assert len(fake.dispatched) == 1 and fake.dispatched[0][1] == f"{upload_job.id.hex}:1"
    assert ready.runpod_job_id == "rp-1"
    names = [e.event for e in await job_repo.list_events(upload_job.id)]
    assert names.index("node_expired") < names.index("fallback_runpod")


async def test_non_youtube_url_fails_closed_before_any_node(cloud_settings, job_repo, node_repo):
    await _register(node_repo)
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://vimeo.com/12345")
    orch, task = await _run(cloud_settings)
    try:
        failed = await wait_for_status(job_repo, job.id, JobStage.failed, timeout=10)
    finally:
        await _stop(orch, task)
    assert failed.error_code == "YTDLP_BLOCKED" and failed.attempt == 0
    assert await node_repo.latest_for_job(job.id) is None


async def test_lost_download_report_completes_from_the_s3_marker(
    cloud_settings, job_repo, node_repo, monkeypatch
):
    _stub_source_receipt(monkeypatch, sha256="e" * 64)
    await _register(node_repo)
    job = await job_repo.create_job(source_type=SourceType.url, source_ref="https://youtu.be/dQw4w9WgXcQ")
    orch, task = await _run(cloud_settings)
    try:
        dl = await _wait_assignment(node_repo, job.id, NodeAssignmentKind.download)
        # The node acknowledged, wrote source.json (stubbed present), and its
        # completion report never arrived: the marker still completes it.
        assert await node_repo.acknowledge(dl.id, node_id=MAC)
        job_after = await wait_for_status(job_repo, job.id, JobStage.dispatched, timeout=10)
    finally:
        await _stop(orch, task)
    assert job_after.input_checksum == "e" * 64
    done = await node_repo.get_assignment(dl.id)
    assert done.status == NodeAssignmentStatus.completed
    assert done.detail == {"completed_by": "s3-marker"}
    # The node is free again (it may already hold this job's separation).
    assert (await node_repo.get(MAC)).busy_assignment_id != dl.id


async def test_lost_separation_report_completes_from_handoff(
    cloud_settings, job_repo, node_repo, upload_job, monkeypatch
):
    _stub_cloud_intake(monkeypatch)
    _stub_package_ready(monkeypatch, True)  # handoff.json "exists" from the start
    await _register(node_repo)
    fake = FakeRunPodClient([])
    orch, task = await _run(cloud_settings, fake)
    try:
        sep = await _wait_assignment(node_repo, upload_job.id, NodeAssignmentKind.separate)
        ready = await wait_for_status(job_repo, upload_job.id, JobStage.ready, timeout=10)
    finally:
        await _stop(orch, task)
    assert fake.dispatched == []
    assert (await node_repo.get_assignment(sep.id)).detail == {"completed_by": "s3-marker"}
    assert ready.runpod_job_id == nodes.executor_id(sep.id)

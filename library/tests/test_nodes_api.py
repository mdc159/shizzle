"""Unit: worker-node API (docs/WORKER-NODES.md) via in-process ASGI transport.

Covers node-token auth (E6), heartbeat upsert and the dashboard listing,
the assignment long-poll, and the node-fenced ack/progress/complete/fail
reports. Orchestrator routing lives in test_node_routing.py.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from shizzle_server.api.nodes import require_node
from shizzle_server.db.models import NodeAssignmentKind, NodeAssignmentStatus, SourceType
from shizzle_server.main import create_app

MAC = {"Authorization": "Bearer mac-secret"}
DELL = {"Authorization": "Bearer dell-secret"}


@pytest_asyncio.fixture
async def node_client(settings):
    settings.shizzle_node_tokens = "mac-mini=mac-secret, dell-9530=dell-secret"
    settings.node_online_seconds = 45.0
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, app


async def _claimed_job(app, *, worker_id: str = "orch-test"):
    """A URL job claimed by ``worker_id`` so create_assignment's lease fence passes."""
    repo = app.state.job_repo
    job = await repo.create_job(source_type=SourceType.url, source_ref="https://youtu.be/abc")
    claimed = await repo.claim_next(worker_id=worker_id, lease_seconds=60)
    assert claimed is not None and claimed.id == job.id
    return job


# --- auth ----------------------------------------------------------------------


async def test_heartbeat_requires_the_nodes_own_token(node_client):
    c, _ = node_client
    body = {"capabilities": {"download": True}}
    assert (await c.post("/api/nodes/mac-mini/heartbeat", json=body)).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert (await c.post("/api/nodes/mac-mini/heartbeat", json=body, headers=bad)).status_code == 401
    # Another node's valid token is not this node's token.
    assert (
        await c.post("/api/nodes/mac-mini/heartbeat", json=body, headers=DELL)
    ).status_code == 401
    # Unknown node id answers the same 401 (no existence oracle).
    assert (
        await c.post("/api/nodes/ghost/heartbeat", json=body, headers=MAC)
    ).status_code == 401
    ok = await c.post("/api/nodes/mac-mini/heartbeat", json=body, headers=MAC)
    assert ok.status_code == 200 and ok.json()["ok"] is True


async def test_node_token_does_not_open_passcode_routes(settings, tmp_path):
    """E6: a node token is node-scoped; the library route still wants the passcode token."""
    settings.shizzle_passcode = "open-sesame"
    settings.shizzle_node_tokens = "mac-mini=mac-secret"
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c,
    ):
        assert (await c.get("/api/library", headers=MAC)).status_code == 401
        assert (await c.get("/api/nodes", headers=MAC)).status_code == 401
        # And the node route still works with the node token while the gate is on.
        r = await c.post("/api/nodes/mac-mini/heartbeat", json={"capabilities": {}}, headers=MAC)
        assert r.status_code == 200


# --- registry ------------------------------------------------------------------


async def test_heartbeat_upserts_and_listing_reports_online(node_client):
    c, app = node_client
    r = await c.post(
        "/api/nodes/mac-mini/heartbeat",
        json={
            "displayName": "Mac mini",
            "capabilities": {"download": True, "separate": True, "device": "mps"},
            "agentVersion": "0.1.0",
        },
        headers=MAC,
    )
    assert r.status_code == 200
    listing = (await c.get("/api/nodes")).json()
    assert listing["onlineSeconds"] == 45.0
    [node] = listing["nodes"]
    assert node["id"] == "mac-mini"
    assert node["displayName"] == "Mac mini"
    assert node["online"] is True
    assert node["disabled"] is False
    assert node["capabilities"]["device"] == "mps"
    assert node["busyAssignmentId"] is None

    # A second heartbeat updates, never duplicates.
    await c.post(
        "/api/nodes/mac-mini/heartbeat",
        json={"capabilities": {"download": False}},
        headers=MAC,
    )
    nodes = await app.state.node_repo.list_nodes()
    assert len(nodes) == 1 and nodes[0].capabilities == {"download": False}


async def test_listing_marks_a_stale_node_offline(node_client):
    c, app = node_client
    await c.post("/api/nodes/mac-mini/heartbeat", json={"capabilities": {}}, headers=MAC)
    from datetime import timedelta

    from shizzle_server.db.models import Node, utcnow

    sf = app.state.node_repo._sf
    async with sf() as session, session.begin():
        node = await session.get(Node, "mac-mini")
        node.last_seen_at = utcnow() - timedelta(seconds=300)
    [node_info] = (await c.get("/api/nodes")).json()["nodes"]
    assert node_info["online"] is False


# --- assignments -------------------------------------------------------------


async def test_next_assignment_is_204_until_the_orchestrator_assigns(node_client):
    c, app = node_client
    await c.post("/api/nodes/mac-mini/heartbeat", json={"capabilities": {}}, headers=MAC)
    assert (await c.get("/api/nodes/mac-mini/assignments/next", headers=MAC)).status_code == 204
    # A short long-poll returns 204 after the wait elapses.
    r = await c.get("/api/nodes/mac-mini/assignments/next?wait=1", headers=MAC)
    assert r.status_code == 204

    job = await _claimed_job(app)
    assignment = await app.state.node_repo.create_assignment(
        job.id,
        worker_id="orch-test",
        node_id="mac-mini",
        kind=NodeAssignmentKind.download,
        payload={"url": "https://youtu.be/abc", "bucket": "b"},
    )
    assert assignment is not None
    r = await c.get("/api/nodes/mac-mini/assignments/next", headers=MAC)
    assert r.status_code == 200
    body = r.json()
    assert body["assignmentId"] == str(assignment.id)
    assert body["jobId"] == job.id.hex
    assert body["kind"] == "download"
    assert body["status"] == "assigned"
    assert body["payload"]["url"] == "https://youtu.be/abc"
    # The node row is busy and the job records its executor.
    node = await app.state.node_repo.get("mac-mini")
    assert node.busy_assignment_id == assignment.id
    assert (await app.state.job_repo.get_job(job.id)).executor == "node:mac-mini"
    events = [e.event for e in await app.state.job_repo.list_events(job.id)]
    assert "node_assigned" in events


async def test_ack_progress_complete_flow_frees_the_node(node_client):
    c, app = node_client
    await c.post("/api/nodes/mac-mini/heartbeat", json={"capabilities": {}}, headers=MAC)
    job = await _claimed_job(app)
    repo = app.state.node_repo
    assignment = await repo.create_assignment(
        job.id,
        worker_id="orch-test",
        node_id="mac-mini",
        kind=NodeAssignmentKind.download,
        payload={},
    )
    base = f"/api/nodes/mac-mini/assignments/{assignment.id}"
    assert (await c.post(f"{base}/ack", headers=MAC)).status_code == 200
    assert (await repo.get_assignment(assignment.id)).status == NodeAssignmentStatus.acknowledged
    r = await c.post(f"{base}/progress", json={"phase": "download: 40%"}, headers=MAC)
    assert r.status_code == 200
    current = await repo.get_assignment(assignment.id)
    assert current.status == NodeAssignmentStatus.running and current.phase == "download: 40%"
    r = await c.post(
        f"{base}/complete", json={"detail": {"sha256": "a" * 64, "bytes": 10}}, headers=MAC
    )
    assert r.status_code == 200
    done = await repo.get_assignment(assignment.id)
    assert done.status == NodeAssignmentStatus.completed
    assert done.detail == {"sha256": "a" * 64, "bytes": 10}
    assert (await repo.get("mac-mini")).busy_assignment_id is None
    # Terminal assignments reject further reports.
    assert (await c.post(f"{base}/progress", json={"phase": "late"}, headers=MAC)).status_code == 409
    assert (await c.get("/api/nodes/mac-mini/assignments/next", headers=MAC)).status_code == 204


async def test_only_the_named_node_can_report(node_client):
    c, app = node_client
    for node_id, headers in (("mac-mini", MAC), ("dell-9530", DELL)):
        await c.post(f"/api/nodes/{node_id}/heartbeat", json={"capabilities": {}}, headers=headers)
    job = await _claimed_job(app)
    assignment = await app.state.node_repo.create_assignment(
        job.id, worker_id="orch-test", node_id="mac-mini", kind=NodeAssignmentKind.separate,
        payload={}, idempotency_key="k1",
    )
    # The Dell, with its own valid token, cannot touch the Mac's assignment.
    r = await c.post(f"/api/nodes/dell-9530/assignments/{assignment.id}/ack", headers=DELL)
    assert r.status_code == 409
    assert (await c.get("/api/nodes/dell-9530/assignments/next", headers=DELL)).status_code == 204
    # An unknown assignment id is a 409 (not open for this node); a malformed
    # id is a 404. Neither is a 500.
    r = await c.post(f"/api/nodes/mac-mini/assignments/{uuid.uuid4()}/ack", headers=MAC)
    assert r.status_code == 409
    r = await c.post("/api/nodes/mac-mini/assignments/not-a-uuid/ack", headers=MAC)
    assert r.status_code == 404


async def test_fail_report_records_detail_and_frees_the_node(node_client):
    c, app = node_client
    await c.post("/api/nodes/mac-mini/heartbeat", json={"capabilities": {}}, headers=MAC)
    job = await _claimed_job(app)
    repo = app.state.node_repo
    assignment = await repo.create_assignment(
        job.id, worker_id="orch-test", node_id="mac-mini", kind=NodeAssignmentKind.download,
        payload={},
    )
    r = await c.post(
        f"/api/nodes/mac-mini/assignments/{assignment.id}/fail",
        json={"code": "YTDLP_BLOCKED", "detail": "Sign in to confirm you're not a bot"},
        headers=MAC,
    )
    assert r.status_code == 200
    failed = await repo.get_assignment(assignment.id)
    assert failed.status == NodeAssignmentStatus.failed
    assert failed.detail["code"] == "YTDLP_BLOCKED"
    assert (await repo.get("mac-mini")).busy_assignment_id is None


async def test_heartbeat_adopts_only_an_open_assignment_held_by_this_node(node_client):
    """After the watchdog expired an assignment, the agent's next heartbeat must
    not re-mark the node busy with that dead id (nothing would ever clear it)."""
    c, app = node_client
    repo = app.state.node_repo
    await c.post("/api/nodes/mac-mini/heartbeat", json={"capabilities": {}}, headers=MAC)
    await c.post("/api/nodes/dell-9530/heartbeat", json={"capabilities": {}}, headers=DELL)
    job = await _claimed_job(app, worker_id="w1")
    held = await repo.create_assignment(
        job.id, worker_id="w1", node_id="mac-mini", kind=NodeAssignmentKind.download, payload={}
    )
    assert await repo.expire(held.id, reason="stalled")
    assert (await repo.get("mac-mini")).busy_assignment_id is None
    # The agent still thinks it holds it.
    r = await c.post(
        "/api/nodes/mac-mini/heartbeat", json={"busyAssignmentId": str(held.id)}, headers=MAC
    )
    assert r.status_code == 200
    assert (await repo.get("mac-mini")).busy_assignment_id is None
    # A random id, or another node's open assignment, is not adopted either.
    await c.post("/api/nodes/mac-mini/heartbeat", json={"busyAssignmentId": str(uuid.uuid4())}, headers=MAC)
    assert (await repo.get("mac-mini")).busy_assignment_id is None
    second = await _claimed_job(app, worker_id="w2")
    theirs = await repo.create_assignment(
        second.id, worker_id="w2", node_id="dell-9530", kind=NodeAssignmentKind.download, payload={}
    )
    await c.post("/api/nodes/mac-mini/heartbeat", json={"busyAssignmentId": str(theirs.id)}, headers=MAC)
    assert (await repo.get("mac-mini")).busy_assignment_id is None
    # A heartbeat without capabilities keeps the stored set.
    await c.post(
        "/api/nodes/mac-mini/heartbeat", json={"capabilities": {"download": True}}, headers=MAC
    )
    await c.post("/api/nodes/mac-mini/heartbeat", json={}, headers=MAC)
    assert (await repo.get("mac-mini")).capabilities == {"download": True}


async def test_disabled_node_is_offered_nothing_and_completion_detail_is_bounded(node_client):
    c, app = node_client
    repo = app.state.node_repo
    await c.post("/api/nodes/mac-mini/heartbeat", json={"capabilities": {}}, headers=MAC)
    job = await _claimed_job(app, worker_id="w1")
    held = await repo.create_assignment(
        job.id, worker_id="w1", node_id="mac-mini", kind=NodeAssignmentKind.download, payload={}
    )
    assert (await c.get("/api/nodes/mac-mini/assignments/next", headers=MAC)).status_code == 200
    # Kill switch after the fact: pending work is withheld too.
    assert await repo.set_disabled("mac-mini", True)
    assert (await c.get("/api/nodes/mac-mini/assignments/next", headers=MAC)).status_code == 204
    assert await repo.set_disabled("mac-mini", False)
    assert (await c.get("/api/nodes/mac-mini/assignments/next", headers=MAC)).status_code == 200
    # A completion report is a hint and stays small.
    r = await c.post(
        f"/api/nodes/mac-mini/assignments/{held.id}/complete",
        json={"detail": {"blob": "x" * 9000}},
        headers=MAC,
    )
    assert r.status_code == 422
    # A non-ASCII token is a 401, not a 500 (hmac.compare_digest on str would
    # raise TypeError). The HTTP client cannot even send such a header, so the
    # dependency is exercised directly.
    fake_request = SimpleNamespace(headers={"authorization": "Bearer t\u00f6ken"}, app=app)
    with pytest.raises(HTTPException) as refused:
        require_node(fake_request, "mac-mini")
    assert refused.value.status_code == 401


async def test_superseded_separation_assignment_is_expired_under_the_lease(node_client):
    """B16 cleanup before a fresh dispatch: an open assignment from an earlier
    attempt is expired, its node freed and a node_expired event recorded, but
    only by the handler that still holds the job's lease (B2)."""
    from types import SimpleNamespace as NS

    from shizzle_server.orchestrator import nodes as nodes_mod

    c, app = node_client
    repo, jobs = app.state.node_repo, app.state.job_repo
    await c.post("/api/nodes/mac-mini/heartbeat", json={"capabilities": {}}, headers=MAC)
    job = await _claimed_job(app, worker_id="w1")
    old = await repo.create_assignment(
        job.id, worker_id="w1", node_id="mac-mini", kind=NodeAssignmentKind.separate,
        payload={}, idempotency_key=f"{job.id.hex}:0",
    )
    assert (await repo.get("mac-mini")).busy_assignment_id == old.id
    job_row = await jobs.get_job(job.id)
    fresh_key = f"{job.id.hex}:{job_row.attempt + 1}"

    # A handler that does not hold the lease changes nothing.
    stranger = NS(job=NS(id=job.id, attempt=job_row.attempt + 1), nodes=repo, jobs=jobs, worker_id="w2")
    await nodes_mod.expire_superseded_assignments(stranger, dispatch_key=fresh_key)
    assert (await repo.get_assignment(old.id)).status == NodeAssignmentStatus.assigned
    assert (await repo.get("mac-mini")).busy_assignment_id == old.id

    owner = NS(job=NS(id=job.id, attempt=job_row.attempt + 1), nodes=repo, jobs=jobs, worker_id="w1")
    await nodes_mod.expire_superseded_assignments(owner, dispatch_key=fresh_key)
    expired = await repo.get_assignment(old.id)
    assert expired.status == NodeAssignmentStatus.expired
    assert expired.detail["expired_reason"].startswith("superseded by attempt")
    assert (await repo.get("mac-mini")).busy_assignment_id is None
    events = [e for e in await jobs.list_events(job.id) if e.event == "node_expired"]
    assert len(events) == 1 and events[0].detail["assignment_id"] == str(old.id)
    # The current attempt's own assignment is never touched.
    await nodes_mod.expire_superseded_assignments(owner, dispatch_key=f"{job.id.hex}:0")
    assert (await repo.get_assignment(old.id)).status == NodeAssignmentStatus.expired


async def test_create_assignment_refuses_a_busy_or_disabled_node(node_client):
    c, app = node_client
    await c.post("/api/nodes/mac-mini/heartbeat", json={"capabilities": {}}, headers=MAC)
    repo = app.state.node_repo
    first = await _claimed_job(app, worker_id="w1")
    held = await repo.create_assignment(
        first.id, worker_id="w1", node_id="mac-mini", kind=NodeAssignmentKind.download, payload={}
    )
    assert held is not None
    second = await _claimed_job(app, worker_id="w2")
    assert (
        await repo.create_assignment(
            second.id, worker_id="w2", node_id="mac-mini", kind=NodeAssignmentKind.download,
            payload={},
        )
    ) is None
    # Expiring the held assignment frees the slot; a disabled node still refuses.
    assert await repo.expire(held.id, reason="test") is True
    assert (await repo.get("mac-mini")).busy_assignment_id is None
    assert await repo.set_disabled("mac-mini", True) is True
    assert (
        await repo.create_assignment(
            second.id, worker_id="w2", node_id="mac-mini", kind=NodeAssignmentKind.download,
            payload={},
        )
    ) is None


async def test_create_assignment_requires_the_job_lease(node_client):
    c, app = node_client
    await c.post("/api/nodes/mac-mini/heartbeat", json={"capabilities": {}}, headers=MAC)
    job = await _claimed_job(app, worker_id="owner")
    from shizzle_server.db.repository import InvalidTransition

    with pytest.raises(InvalidTransition):
        await app.state.node_repo.create_assignment(
            job.id, worker_id="impostor", node_id="mac-mini", kind=NodeAssignmentKind.download,
            payload={},
        )
    assert (await app.state.node_repo.get("mac-mini")).busy_assignment_id is None

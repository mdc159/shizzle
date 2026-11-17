"""Administrator API (docs/INVARIANTS.md G-section) via in-process ASGI.

Covers: fail-closed auth (404 without the proxy secret header), E2 (no
source_ref anywhere, assignment payloads excluded), the jobs list with
counts/flags, the job-detail evidence trace, node enable/disable, runtime
settings (env fallback, validation, RunPod-at-work count) and the
assignment release path. The S3 verification path is exercised against
moto; S3 errors are monkeypatched to prove they yield ``unknown``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

from shizzle_server.api.media import _s3_client
from shizzle_server.db.models import (
    Job,
    JobStage,
    NodeAssignmentKind,
    NodeAssignmentStatus,
    SourceType,
    utcnow,
)
from shizzle_server.db.repository import NodeRepository, track_id_for_job
from shizzle_server.errors import ErrorCode
from shizzle_server.main import create_app
from shizzle_server.settings import Settings

SECRET = "tail-scale-proxy-secret"
HEADERS = {"x-shizzle-admin": SECRET}
# A URL with a query credential: E2 says this string must never come back.
CRED_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ&token=supersecret-token"
CRED_FRAGMENT = "supersecret-token"


@pytest_asyncio.fixture
async def admin_client(settings):
    settings.shizzle_admin_proxy_secret = SECRET
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, app


@pytest_asyncio.fixture
async def node_repo(session_factory) -> NodeRepository:
    return NodeRepository(session_factory)


async def _make_job(app, *, source_type: SourceType = SourceType.upload, **kwargs):
    return await app.state.job_repo.create_job(
        source_type=source_type,
        source_ref=kwargs.pop("source_ref", "source.mp4"),
        **kwargs,
    )


async def _set_job(session_factory, job_id, **fields):
    async with session_factory() as session, session.begin():
        job = await session.get(Job, job_id)
        for key, value in fields.items():
            setattr(job, key, value)


# --- auth: fail closed ----------------------------------------------------------


async def test_admin_routes_404_without_the_secret_header(admin_client):
    c, _ = admin_client
    assert (await c.get("/api/admin/jobs")).status_code == 404
    wrong = {"x-shizzle-admin": "not-the-secret"}
    assert (await c.get("/api/admin/jobs", headers=wrong)).status_code == 404
    assert (await c.get("/api/admin/nodes")).status_code == 404
    assert (await c.get("/api/admin/settings")).status_code == 404
    assert (await c.post("/api/admin/nodes/mac/enable", headers=wrong)).status_code == 404
    assert (await c.get("/api/admin/jobs/{uuid.uuid4()}", headers=wrong)).status_code == 404


async def test_empty_secret_means_every_admin_route_404s(settings):
    """Fail closed: no configured secret, no admin surface at all."""
    settings.shizzle_admin_proxy_secret = ""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await c.get("/api/admin/jobs", headers=HEADERS)).status_code == 404
            assert (await c.get("/api/admin/settings", headers=HEADERS)).status_code == 404


async def test_correct_header_is_accepted(admin_client):
    c, _ = admin_client
    response = await c.get("/api/admin/jobs", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["jobs"] == []


async def test_admin_open_skips_the_header_check(settings):
    """SHIZZLE_ADMIN_OPEN=1: local dev/tests only."""
    settings.shizzle_admin_proxy_secret = ""
    settings.shizzle_admin_open = True
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            assert (await c.get("/api/admin/jobs")).status_code == 200


# --- jobs list --------------------------------------------------------------------


async def test_jobs_list_carries_failure_and_control_plane_detail(admin_client):
    c, app = admin_client
    job = await _make_job(app, title="Boom")
    await _set_job(
        _sf(app),
        job.id,
        status=JobStage.failed,
        error_code="RUNPOD_DISPATCH_FAILED",
        error_detail="RunPod job rp-9 FAILED: boom",
        attempt=2,
        executor="runpod",
    )
    response = await c.get("/api/admin/jobs", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    row = body["jobs"][0]
    assert row["errorCode"] == "RUNPOD_DISPATCH_FAILED"
    assert "rp-9 FAILED: boom" in row["errorDetail"]
    assert row["attempt"] == 2 and row["executor"] == "runpod"
    for field in ("leaseOwner", "leaseExpiresAt", "nextRetryAt", "createdAt", "updatedAt", "flags"):
        assert field in row
    assert body["counts"].get("failed") == 1

    # Status filter returns only failures and keeps the counts global.
    filtered = await c.get("/api/admin/jobs?status=failed", headers=HEADERS)
    assert filtered.status_code == 200
    fbody = filtered.json()
    assert [r["status"] for r in fbody["jobs"]] == ["failed"]
    assert fbody["counts"].get("failed") == 1
    assert (await c.get("/api/admin/jobs?status=nonsense", headers=HEADERS)).status_code == 422


def _sf(app):
    """The app's session factory, reached through a repository (state-only)."""
    return app.state.job_repo._sf


# --- E2: source_ref never exposed ---------------------------------------------------


async def test_no_admin_response_ever_carries_source_ref(admin_client):
    """G2/E2: a credential-bearing submitted URL must not leak anywhere."""
    c, app = admin_client
    job = await _make_job(
        app, source_type=SourceType.url, source_ref=CRED_URL, title="Secret"
    )
    for path in (
        "/api/admin/jobs",
        f"/api/admin/jobs/{job.id}",
        f"/api/admin/jobs/{job.id}?verify=s3",
        "/api/admin/nodes",
        "/api/admin/settings",
    ):
        response = await c.get(path, headers=HEADERS)
        assert response.status_code == 200, path
        assert CRED_FRAGMENT not in response.text, path
        assert "source_ref" not in [
            k.lower() for k in _walk_keys(response.json())
        ], path


def _walk_keys(value):
    keys = []
    if isinstance(value, dict):
        for k, v in value.items():
            keys.append(k)
            keys.extend(_walk_keys(v))
    elif isinstance(value, list):
        for item in value:
            keys.extend(_walk_keys(item))
    return keys


async def test_job_detail_excludes_assignment_payloads(admin_client):
    """The assignment payload carries the source URL (E6): never returned."""
    c, app = admin_client
    await app.state.node_repo.heartbeat(
        "mac-mini", display_name="Mac mini", capabilities={"download": True}
    )
    job = await _make_job(
        app, source_type=SourceType.url, source_ref=CRED_URL, title="T"
    )
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=60)
    assert claimed is not None and claimed.id == job.id
    created = await app.state.node_repo.create_assignment(
        job.id,
        worker_id="w",
        node_id="mac-mini",
        kind=NodeAssignmentKind.download,
        payload={"url": CRED_URL, "source_ref": "youtube-x"},
    )
    assert created is not None

    response = await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["assignments"][0]["id"] == str(created.id)
    assert CRED_FRAGMENT not in response.text
    assert all("payload" not in a for a in body["assignments"])


# --- job detail: the trace -----------------------------------------------------------


async def test_job_detail_is_the_full_trace(admin_client):
    c, app = admin_client
    job = await _make_job(app, title="Trace me")
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=60)
    assert claimed.id == job.id
    await app.state.job_repo.advance(
        job.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )

    response = await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["job"]["jobId"] == job.id.hex
    assert body["job"]["idempotencyKey"] == job.idempotency_key
    assert body["derivedTrackId"] == str(track_id_for_job(job.id))
    assert body["dispatchAttempt"]["idempotencyKey"] == f"{job.id.hex}:0"
    names = [e["event"] for e in body["events"]]
    assert names == ["created", "stage_completed"]
    assert body["assignments"] == []
    assert body["track"] is None
    statuses = {item["name"]: item["status"] for item in body["evidence"]}
    assert statuses["created_event"] == "ok"
    assert statuses["dispatch_identity"] == "not_yet"
    assert statuses["track_row"] == "not_yet"
    # Every evidence item carries the full shape.
    for item in body["evidence"]:
        assert set(item) == {"stage", "name", "expected", "observed", "proof", "checkedAt", "status"}
        assert item["status"] in ("ok", "missing", "mismatch", "not_yet", "unknown")
    assert (await c.get(f"/api/admin/jobs/{uuid.uuid4()}", headers=HEADERS)).status_code == 404


async def test_ready_without_a_track_row_is_flagged(admin_client):
    c, app = admin_client
    job = await _make_job(app, title="Phantom")
    await _set_job(_sf(app), job.id, status=JobStage.ready)
    response = await c.get("/api/admin/jobs", headers=HEADERS)
    assert response.json()["jobs"][0]["flags"] == ["ready_without_track"]
    detail = await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)
    track_row = next(
        i for i in detail.json()["evidence"] if i["name"] == "track_row"
    )
    assert track_row["status"] == "missing"


async def test_open_assignment_on_a_terminal_job_is_flagged_and_releasable(
    admin_client, session_factory
):
    c, app = admin_client
    await app.state.node_repo.heartbeat(
        "mac-mini", capabilities={"download": True}
    )
    job = await _make_job(app, source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c")
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=60)
    assert claimed.id == job.id
    assignment = await app.state.node_repo.create_assignment(
        job.id,
        worker_id="w",
        node_id="mac-mini",
        kind=NodeAssignmentKind.download,
        payload={"url": "https://youtu.be/x1y2z3a4b5c"},
    )
    assert assignment is not None
    # The job fails elsewhere; the assignment (and the node's busy pointer) leak.
    await _set_job(session_factory, job.id, status=JobStage.failed, error_code="NODE_FAILED")

    listing = await c.get("/api/admin/jobs", headers=HEADERS)
    flags = listing.json()["jobs"][0]["flags"]
    assert "open_assignment_on_terminal_job" in flags

    detail = await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)
    assert detail.status_code == 200

    release = await c.post(f"/api/admin/assignments/{assignment.id}/release", headers=HEADERS)
    assert release.status_code == 200
    body = release.json()
    assert body["ok"] is True and body["nodeId"] == "mac-mini"
    fresh = await app.state.node_repo.get_assignment(assignment.id)
    assert fresh.status == NodeAssignmentStatus.expired
    assert "released by an administrator" in fresh.detail["expired_reason"]
    node = await app.state.node_repo.get("mac-mini")
    assert node.busy_assignment_id is None
    events = await app.state.job_repo.list_events(job.id)
    released = [e for e in events if e.event == "admin_assignment_released"]
    assert len(released) == 1
    assert released[0].detail["assignment_id"] == str(assignment.id)

    # A closed assignment cannot be released again.
    again = await c.post(f"/api/admin/assignments/{assignment.id}/release", headers=HEADERS)
    assert again.status_code == 409


async def test_release_refuses_live_jobs_and_unknown_ids(admin_client):
    c, app = admin_client
    await app.state.node_repo.heartbeat("mac-mini", capabilities={"download": True})
    job = await _make_job(app, source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c")
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=60)
    assert claimed.id == job.id
    assignment = await app.state.node_repo.create_assignment(
        job.id,
        worker_id="w",
        node_id="mac-mini",
        kind=NodeAssignmentKind.download,
        payload={},
    )
    live = await c.post(f"/api/admin/assignments/{assignment.id}/release", headers=HEADERS)
    assert live.status_code == 409
    assert (await c.post(f"/api/admin/assignments/{uuid.uuid4()}/release", headers=HEADERS)).status_code == 404
    assert (
        await c.post("/api/admin/assignments/not-a-uuid/release", headers=HEADERS)
    ).status_code == 404


async def test_stalled_assignment_and_overdue_ack_flags(admin_client, session_factory):
    c, app = admin_client
    settings: Settings = app.state.settings
    await app.state.node_repo.heartbeat("mac-mini", capabilities={"separate": True})
    job = await _make_job(app, title="Stuck")
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=60)
    assert claimed.id == job.id
    await app.state.job_repo.advance(
        job.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await app.state.job_repo.advance(
        job.id, worker_id="w", from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    assignment = await app.state.node_repo.create_assignment(
        job.id,
        worker_id="w",
        node_id="mac-mini",
        kind=NodeAssignmentKind.separate,
        payload={},
        idempotency_key=f"{job.id.hex}:0",
    )
    # Pretend the assignment was created long ago with a stale heartbeat,
    # past the ack window, the stall window and the absolute cap.
    stale = utcnow() - timedelta(seconds=settings.node_assignment_max_seconds + 60)
    async with session_factory() as session, session.begin():
        row = await session.get(type(assignment), assignment.id)
        row.created_at = stale
        row.heartbeat_at = stale
        row.updated_at = stale

    listing = await c.get("/api/admin/jobs", headers=HEADERS)
    flags = listing.json()["jobs"][0]["flags"]
    assert "assignment_ack_overdue" in flags
    assert "assignment_stalled" in flags
    assert "assignment_over_cap" in flags


async def test_expired_lease_on_runnable_work_is_flagged(admin_client):
    c, app = admin_client
    job = await _make_job(app, title="Leaky lease")
    expired = utcnow() - timedelta(seconds=10)
    await _set_job(_sf(app), job.id, status=JobStage.downloading, lease_owner="gone", lease_expires_at=expired)
    listing = await c.get("/api/admin/jobs", headers=HEADERS)
    assert listing.json()["jobs"][0]["flags"] == ["expired_lease_on_runnable"]


# --- nodes --------------------------------------------------------------------------


async def test_nodes_listing_enable_and_disable(admin_client):
    c, app = admin_client
    await app.state.node_repo.heartbeat(
        "mac-mini",
        display_name="Mac mini",
        capabilities={"download": True, "separate": True, "device": "mps"},
        agent_version="0.1.0",
    )
    response = await c.get("/api/admin/nodes", headers=HEADERS)
    assert response.status_code == 200
    node = response.json()["nodes"][0]
    assert node["id"] == "mac-mini" and node["online"] is True and node["enabled"] is True
    assert node["currentAssignment"] is None

    disable = await c.post("/api/admin/nodes/mac-mini/disable", headers=HEADERS)
    assert disable.status_code == 200 and disable.json()["enabled"] is False
    listing = await c.get("/api/admin/nodes", headers=HEADERS)
    assert listing.json()["nodes"][0]["enabled"] is False

    enable = await c.post("/api/admin/nodes/mac-mini/enable", headers=HEADERS)
    assert enable.status_code == 200 and enable.json()["enabled"] is True
    assert (await c.post("/api/admin/nodes/ghost/enable", headers=HEADERS)).status_code == 404


async def test_node_listing_reports_the_current_assignment(admin_client):
    c, app = admin_client
    await app.state.node_repo.heartbeat("mac-mini", capabilities={"download": True})
    job = await _make_job(
        app, source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c"
    )
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=60)
    assert claimed.id == job.id
    assignment = await app.state.node_repo.create_assignment(
        job.id, worker_id="w", node_id="mac-mini",
        kind=NodeAssignmentKind.download, payload={},
    )
    listing = await c.get("/api/admin/nodes", headers=HEADERS)
    node = listing.json()["nodes"][0]
    assert node["busyAssignmentId"] == str(assignment.id)
    assert node["currentAssignment"]["jobId"] == job.id.hex
    assert node["currentAssignment"]["kind"] == "download"


# --- settings ------------------------------------------------------------------------


async def test_settings_report_env_values_and_runpod_state(admin_client, settings):
    c, app = admin_client
    settings.runpod_api_key = "k"
    settings.runpod_endpoint_id = "e"
    response = await c.get("/api/admin/settings", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["nodeRouting"] == {"value": "prefer", "source": "environment", "error": None}
    assert body["runpodMaxConcurrent"]["source"] == "environment"
    assert body["runpodConfigured"] is True
    assert body["jobsAtRunpod"] == 0


async def test_settings_put_stores_validates_and_clears(admin_client):
    c, app = admin_client
    ok = await c.put(
        "/api/admin/settings",
        json={"nodeRouting": "download_only", "runpodMaxConcurrent": 2},
        headers=HEADERS,
    )
    assert ok.status_code == 200
    body = ok.json()
    assert body["nodeRouting"] == {"value": "download_only", "source": "database", "error": None}
    assert body["runpodMaxConcurrent"] == {"value": 2, "source": "database", "error": None}

    invalid = await c.put("/api/admin/settings", json={"nodeRouting": "sideways"}, headers=HEADERS)
    assert invalid.status_code == 422
    # Nothing was stored by the rejected request.
    still = (await c.get("/api/admin/settings", headers=HEADERS)).json()
    assert still["nodeRouting"]["value"] == "download_only"

    cleared = await c.put("/api/admin/settings", json={"nodeRouting": None}, headers=HEADERS)
    assert cleared.status_code == 200
    assert cleared.json()["nodeRouting"]["source"] == "environment"

    empty = await c.put("/api/admin/settings", json={}, headers=HEADERS)
    assert empty.status_code == 422


async def test_settings_count_jobs_at_runpod(admin_client, session_factory):
    c, app = admin_client
    runpod_job = await _make_job(app, title="At RunPod")
    node_job = await _make_job(app, title="On a node")
    await _set_job(
        session_factory,
        runpod_job.id,
        status=JobStage.dispatched,
        runpod_job_id="rp-77",
        executor="runpod",
    )
    await _set_job(
        session_factory,
        node_job.id,
        status=JobStage.dispatched,
        runpod_job_id=f"node:{uuid.uuid4()}",
        executor="node:mac-mini",
    )
    body = (await c.get("/api/admin/settings", headers=HEADERS)).json()
    assert body["jobsAtRunpod"] == 1


# --- ?verify=s3 (moto) ------------------------------------------------------------------


@pytest.fixture
def aws(monkeypatch):
    for var, value in {
        "AWS_ACCESS_KEY_ID": "testing" * 5,
        "AWS_SECRET_ACCESS_KEY": "testing" * 6,
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(var, value)
    for var in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)


async def _published_job(app, session_factory):
    """A job claimed, published (track row + ready) with an active generation."""
    job = await app.state.job_repo.create_job(
        source_type=SourceType.upload, source_ref="source.mp4", title="Published",
        input_checksum="a" * 64,
    )
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed.id == job.id
    for from_stage, to_stage in (
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.dispatched),
        (JobStage.dispatched, JobStage.verifying),
        (JobStage.verifying, JobStage.publishing),
    ):
        await app.state.job_repo.advance(
            job.id, worker_id="w", from_stage=from_stage, to_stage=to_stage
        )
    track_id = track_id_for_job(job.id)
    await app.state.job_repo.publish_track(
        job.id,
        worker_id="w",
        title="Published",
        artist="",
        duration_seconds=30.0,
        s3_prefix=f"tracks/{track_id}/1",
        manifest_key=f"tracks/{track_id}/1/manifest.json",
        generation=1,
        integrity={"publisher": {"objects": [
            {"file": "manifest.json", "size_bytes": 10, "sha256": None, "sha256_source": "size_only", "ok": True},
            {"file": "video.mp4", "size_bytes": 20, "sha256": None, "sha256_source": "size_only", "ok": True},
        ]}},
    )
    return job, track_id


@pytest.mark.asyncio
async def test_verify_s3_reports_ok_missing_and_mismatch(
    admin_client, aws, session_factory
):
    with mock_aws():
        await _run_verify_s3_checks(admin_client, session_factory)


async def test_verify_s3_checksum_mismatch_is_flagged(admin_client, session_factory, monkeypatch):
    """A stored checksum that contradicts jobs.input_checksum is a mismatch."""
    import base64 as b64mod
    import hashlib

    c, app = admin_client
    job = await _make_job(app, title="Corrupt source", input_checksum="a" * 64)
    await _set_job(session_factory, job.id, status=JobStage.dispatched, executor="runpod")
    from shizzle_server.api import admin as admin_module
    from shizzle_server.db.repository import track_id_for_job
    from shizzle_server.orchestrator import cloud

    wrong = b64mod.b64encode(hashlib.sha256(b"other bytes").digest()).decode()

    def _synthetic(settings, key):
        if key == cloud.source_key(track_id_for_job(job.id)):
            return admin_module._HeadOutcome(
                {"ContentLength": 12, "ChecksumSHA256": wrong}, None
            )
        return admin_module._HeadOutcome(None, None)

    monkeypatch.setattr(admin_module, "_head_blocking", _synthetic)
    response = await c.get(f"/api/admin/jobs/{job.id}?verify=s3", headers=HEADERS)
    evidence = {i["name"]: i for i in response.json()["evidence"]}
    source = evidence["source"]
    assert source["status"] == "mismatch"
    assert "sha256" in source["observed"]


async def _run_verify_s3_checks(admin_client, session_factory):
    _s3_client.cache_clear()
    c, app = admin_client
    settings: Settings = app.state.settings
    job, track_id = await _published_job(app, session_factory)
    import boto3

    s3 = boto3.client("s3", region_name=settings.aws_region)
    s3.create_bucket(Bucket=settings.s3_media_bucket)
    # Source exists with the recorded checksum; manifest and video exist; one
    # stem is absent; video is the wrong size vs the recorded publisher report.
    import hashlib

    from shizzle_server.orchestrator import cloud

    body = b"source-bytes"
    checksum = hashlib.sha256(body).hexdigest()
    await _set_job(session_factory, job.id, input_checksum=checksum)
    s3.put_object(
        Bucket=settings.s3_media_bucket,
        Key=cloud.source_key(track_id),
        Body=body,
        ChecksumSHA256=__import__("base64").b64encode(hashlib.sha256(body).digest()).decode(),
    )
    prefix = f"tracks/{track_id}/1"
    s3.put_object(Bucket=settings.s3_media_bucket, Key=f"{prefix}/manifest.json", Body=b"m" * 10)
    s3.put_object(Bucket=settings.s3_media_bucket, Key=f"{prefix}/video.mp4", Body=b"v" * 21)
    s3.put_object(Bucket=settings.s3_media_bucket, Key=f"{prefix}/stems/vocals.m4a", Body=b"a")

    response = await c.get(f"/api/admin/jobs/{job.id}?verify=s3", headers=HEADERS)
    assert response.status_code == 200
    evidence = {(i["stage"], i["name"]): i for i in response.json()["evidence"]}

    source = evidence[("downloading", "source")]
    assert source["status"] == "ok"
    # moto does not surface ChecksumSHA256 on HEAD; the comparison path with
    # real stored checksums is covered by the synthetic-head test below.
    assert "no stored checksum to compare" in source["observed"]
    manifest = evidence[("ready", "manifest")]
    assert manifest["status"] == "ok"
    video = evidence[("ready", "video")]
    assert video["status"] == "mismatch" and "size" in video["observed"]
    missing_stem = evidence[("ready", "browser_stem_drums")]
    assert missing_stem["status"] == "missing"
    # The attempt package was never produced for this job's selected prefix.
    handoff = evidence[("dispatched", "handoff")]
    assert handoff["status"] == "missing"
    _s3_client.cache_clear()


async def test_verify_s3_yields_unknown_on_s3_errors(admin_client, session_factory, monkeypatch):
    """An S3 error is never ok or missing."""
    c, app = admin_client
    job = await _make_job(app, title="S3 down")
    await _set_job(session_factory, job.id, status=JobStage.dispatched, executor="runpod")
    from shizzle_server.api import admin as admin_module

    def _boom(settings, key):
        return admin_module._HeadOutcome(None, "SimulatedError: bucket unreachable")

    monkeypatch.setattr(admin_module, "_head_blocking", _boom)
    response = await c.get(f"/api/admin/jobs/{job.id}?verify=s3", headers=HEADERS)
    assert response.status_code == 200
    evidence = {i["name"]: i for i in response.json()["evidence"]}
    # The two checks that actually ran against S3 are unknown, never ok or
    # missing; the not-yet-reached items keep their real keys in ``proof``.
    assert evidence["source"]["status"] == "unknown"
    assert evidence["dispatch_receipt"]["status"] == "unknown"
    assert evidence["source"]["observed"] == "S3 check failed: SimulatedError: bucket unreachable"
    assert evidence["handoff"]["status"] == "not_yet"
    _s3_client.cache_clear()


# --- redaction of populated history (G2/E2) --------------------------------------


async def test_populated_history_and_reports_are_redacted(
    admin_client, session_factory, node_repo
):
    """Real pipelines store source_ref-bearing event details (the URL-download
    completion event), node failure text quoting URLs, and integrity blobs.
    None of it may leave an admin route, whatever the database holds."""
    c, app = admin_client
    job = await _make_job(
        app, source_type=SourceType.url, source_ref=CRED_URL, title="History"
    )
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed.id == job.id
    await app.state.job_repo.advance(
        job.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    # Exactly what download_via_node stores on the dispatched transition.
    await app.state.job_repo.advance(
        job.id,
        worker_id="w",
        from_stage=JobStage.downloading,
        to_stage=JobStage.dispatched,
        detail={
            "source": {
                "node_id": "mac-mini",
                "source_ref": "youtube-dQw4w9WgXcQ?token=supersecret-token",
                "url": CRED_URL,
            }
        },
    )
    await node_repo.heartbeat("mac-mini", capabilities={"download": True})
    assignment = await node_repo.create_assignment(
        job.id, worker_id="w", node_id="mac-mini",
        kind=NodeAssignmentKind.download, payload={"url": CRED_URL},
    )
    assert assignment is not None
    assert await node_repo.fail(
        assignment.id,
        node_id="mac-mini",
        detail={"code": "DOWNLOAD_FAILED",
                "detail": f"yt-dlp refused {CRED_URL} (token expired)"},
    )

    response = await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)
    assert response.status_code == 200
    text = response.json()
    body = response.text
    for fragment in (CRED_FRAGMENT, "supersecret-token", "http://", "https://"):
        assert fragment not in body, fragment
    stage = next(
        e for e in text["events"]
        if e["event"] == "stage_completed" and e["detail"].get("to") == "dispatched"
    )
    assert stage["detail"]["source"] == {
        "node_id": "mac-mini",
        "source_ref": "[redacted]",
        "url": "[redacted]",
    }
    reported = text["assignments"][0]["detail"]
    assert reported["code"] == "DOWNLOAD_FAILED"
    assert CRED_FRAGMENT not in reported["detail"]
    assert "[redacted]" in reported["detail"]


async def test_track_integrity_is_redacted(admin_client, session_factory):
    c, app = admin_client
    job, track_id = await _published_job(app, session_factory)
    async with session_factory() as session, session.begin():
        from shizzle_server.db.models import Track

        row = await session.get(Track, track_id)
        row.integrity = {"publisher": {"objects": []}, "signed_url": CRED_URL}
    response = await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)
    assert CRED_FRAGMENT not in response.text
    assert response.json()["track"]["integrity"]["signed_url"] == "[redacted]"


# --- failed-job stage/attempt inference (durable transition history) --------------


async def _failed_at(app, *, at: JobStage, verifying_detail_for=None):
    """A job driven to ``at`` through real transitions, then failed there.

    ``verifying_detail_for(job)`` supplies the dispatched->verifying detail
    (the selected package prefix depends on the job's own id). For
    ``dispatched`` the attempt is confirmed first, so the failure carries a
    durable dispatch identity exactly like production.
    """
    job = await _make_job(app, title=f"Fail at {at.value}")
    verifying_detail = verifying_detail_for(job) if verifying_detail_for else None
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed.id == job.id
    for from_stage, to_stage in (
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.dispatched),
        (JobStage.dispatched, JobStage.verifying),
        (JobStage.verifying, JobStage.publishing),
    ):
        if from_stage == JobStage.dispatched:
            # Leaving dispatched means the attempt was durably dispatched.
            key = f"{job.id.hex}:{job.attempt}"
            assert await app.state.job_repo.reserve_dispatch(
                job.id, worker_id="w", idempotency_key=key
            )
            await app.state.job_repo.confirm_dispatch(
                job.id, idempotency_key=key, runpod_job_id="rp-1"
            )
        await app.state.job_repo.advance(
            job.id,
            worker_id="w",
            from_stage=from_stage,
            to_stage=to_stage,
            detail=verifying_detail if to_stage == JobStage.verifying else None,
        )
        if to_stage == at:
            break
    if at == JobStage.dispatched:
        key = f"{job.id.hex}:{job.attempt}"
        assert await app.state.job_repo.reserve_dispatch(
            job.id, worker_id="w", idempotency_key=key
        )
        await app.state.job_repo.confirm_dispatch(
            job.id, idempotency_key=key, runpod_job_id="rp-1"
        )
    await app.state.job_repo.fail_job(
        job.id, error_code=ErrorCode.PUBLISH_FAILED, error_detail="late failure", worker_id="w"
    )
    return job


async def test_failed_during_dispatched_keeps_its_stages_and_attempt_key(
    admin_client, monkeypatch
):
    c, app = admin_client
    job = await _failed_at(app, at=JobStage.dispatched)
    detail = await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)
    body = detail.json()
    # The failed status did not erase the dispatched stage.
    identity = next(i for i in body["evidence"] if i["name"] == "dispatch_identity")
    assert identity["status"] == "ok"
    assert f"{job.id.hex}:0" in identity["observed"]
    assert body["dispatchAttempt"]["idempotencyKey"] == f"{job.id.hex}:0"
    assert body["dispatchAttempt"]["attemptPrefix"]
    assert body["dispatchAttempt"]["runpodJobId"] == "rp-1"

    # The source transfer already happened: the S3 source check APPLIES.
    from shizzle_server.api import admin as admin_module

    def _present(settings, key):
        return admin_module._HeadOutcome({"ContentLength": 5}, None)

    monkeypatch.setattr(admin_module, "_head_blocking", _present)
    verified = await c.get(f"/api/admin/jobs/{job.id}?verify=s3", headers=HEADERS)
    evidence = {i["name"]: i for i in verified.json()["evidence"]}
    assert evidence["source"]["status"] == "ok"  # applies, not not_yet
    assert evidence["handoff"]["status"] == "not_yet"  # verifying never reached


async def test_failed_during_verifying_and_publishing_use_the_selected_prefix(
    admin_client,
):
    import hashlib

    c, app = admin_client
    def selected_prefix(job):
        return (
            f"tracks/{track_id_for_job(job.id)}/1/separation/attempts/"
            f"{hashlib.sha256(f'{job.id.hex}:0'.encode()).hexdigest()}"
        )

    for at in (JobStage.verifying, JobStage.publishing):
        job = await _failed_at(
            app, at=at,
            verifying_detail_for=lambda j: {"package_prefix": selected_prefix(j)},
        )
        detail = await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)
        body = detail.json()
        # The dispatched->verifying transition's prefix is the identity, and
        # the failed status kept the dispatch evidence reachable.
        assert body["dispatchAttempt"]["attemptPrefix"] == selected_prefix(job)
        assert body["dispatchAttempt"]["idempotencyKey"] == f"{job.id.hex}:0"
        identity = next(i for i in body["evidence"] if i["name"] == "dispatch_identity")
        assert identity["status"] == "ok"


async def test_failed_before_source_transfer_stays_not_yet(admin_client):
    """A job that died during downloading never transferred its source: the
    storage checks report not_yet, not missing."""
    c, app = admin_client
    job = await _make_job(app, title="Early fail")
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed.id == job.id
    await app.state.job_repo.advance(
        job.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await app.state.job_repo.fail_job(
        job.id, error_code=ErrorCode.S3_UPLOAD_FAILED, error_detail="boom", worker_id="w"
    )
    detail = await c.get(f"/api/admin/jobs/{job.id}?verify=s3", headers=HEADERS)
    body = detail.json()
    evidence = {i["name"]: i for i in body["evidence"]}
    assert evidence["source"]["status"] == "not_yet"
    assert evidence["dispatch_identity"]["status"] == "not_yet"
    assert body["dispatchAttempt"]["attemptPrefix"] is None


# --- generation checksums are compared (synthetic HEADs) --------------------------


async def test_verify_s3_generation_checksums_flag_equal_size_corruption(
    admin_client, session_factory, monkeypatch
):
    """Equal-sized objects whose stored checksum contradicts the publisher's
    recorded sha256 are mismatches, not ok."""
    import base64
    import hashlib

    c, app = admin_client
    job, track_id = await _published_job(app, session_factory)
    prefix = f"tracks/{track_id}/1"
    good = base64.b64encode(hashlib.sha256(b"good").digest()).decode()
    bad = base64.b64encode(hashlib.sha256(b"evil").digest()).decode()
    from shizzle_server.api import admin as admin_module

    heads = {
        f"{prefix}/manifest.json": {"ContentLength": 10, "ChecksumSHA256": bad},
        f"{prefix}/video.mp4": {"ContentLength": 20, "ChecksumSHA256": bad},
        f"{prefix}/stems/vocals.m4a": {"ContentLength": 30, "ChecksumSHA256": bad},
        f"{prefix}/stems/drums.m4a": {"ContentLength": 30, "ChecksumSHA256": good},
    }
    # Record the matching sha256 values in the integrity report (b"good").
    digest = hashlib.sha256(b"good").hexdigest()
    async with session_factory() as session, session.begin():
        from shizzle_server.db.models import Track

        row = await session.get(Track, track_id)
        row.integrity = {"publisher": {"objects": [
            {"file": "manifest.json", "size_bytes": 10, "sha256": digest},
            {"file": "video.mp4", "size_bytes": 20, "sha256": digest},
            {"file": "stems/vocals.m4a", "size_bytes": 30, "sha256": digest},
            {"file": "stems/drums.m4a", "size_bytes": 30, "sha256": digest},
        ]}}

    def _synthetic(settings, key):
        head = heads.get(key)
        return admin_module._HeadOutcome(dict(head) if head else None, None)

    monkeypatch.setattr(admin_module, "_head_blocking", _synthetic)
    response = await c.get(f"/api/admin/jobs/{job.id}?verify=s3", headers=HEADERS)
    evidence = {i["name"]: i for i in response.json()["evidence"]}
    assert evidence["manifest"]["status"] == "mismatch"
    assert "sha256" in evidence["manifest"]["observed"]
    assert evidence["video"]["status"] == "mismatch"
    assert evidence["browser_stem_vocals"]["status"] == "mismatch"
    assert evidence["browser_stem_drums"]["status"] == "ok"
    assert "sha256 matches" in evidence["browser_stem_drums"]["observed"]


# --- wait alarms describe outstanding waits only -----------------------------------


async def _backdate(session_factory, model, row_id, *, age_seconds):
    from shizzle_server.db.models import utcnow

    async with session_factory() as session, session.begin():
        row = await session.get(model, row_id)
        row.created_at = utcnow() - timedelta(seconds=age_seconds)


async def test_outstanding_wait_over_window_is_flagged(
    admin_client, session_factory, node_repo
):
    c, app = admin_client
    settings: Settings = app.state.settings
    await node_repo.heartbeat("mac-mini", capabilities={"download": True})
    job = await _make_job(
        app, source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c"
    )
    await app.state.job_repo.append_event(
        job.id, "node_wait", {"kind": "download", "attempt": 0}
    )
    wait_event = next(
        e for e in await app.state.job_repo.list_events(job.id) if e.event == "node_wait"
    )
    await _backdate(
        session_factory, type(wait_event), wait_event.id,
        age_seconds=settings.node_download_wait_seconds + 3600,
    )
    listing = await c.get("/api/admin/jobs", headers=HEADERS)
    flags = listing.json()["jobs"][0]["flags"]
    assert "wait_over_window" in flags


async def test_resolved_waits_are_not_flagged(admin_client, session_factory, node_repo):
    """Old node_wait rows that were resolved — by an assignment and stage
    transition (job now ready), or by an assignment while still waiting —
    describe history, not an outstanding wait."""
    c, app = admin_client
    settings: Settings = app.state.settings
    await node_repo.heartbeat("mac-mini", capabilities={"download": True})

    # A finished job whose download once waited far beyond the window.
    done = await _published_job(app, session_factory)
    await app.state.job_repo.append_event(
        done[0].id, "node_wait", {"kind": "download", "attempt": 0}
    )
    await app.state.job_repo.append_event(
        done[0].id, "node_assigned", {"kind": "download", "node_id": "mac-mini"}
    )
    events = await app.state.job_repo.list_events(done[0].id)
    wait_event = next(e for e in events if e.event == "node_wait")
    await _backdate(
        session_factory, type(wait_event), wait_event.id,
        age_seconds=settings.node_download_wait_seconds + 86400,
    )

    # An in-flight job whose wait was resolved by an assignment (re-parked).
    waiting = await _make_job(
        app, source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c"
    )
    await app.state.job_repo.append_event(
        waiting.id, "node_wait", {"kind": "download", "attempt": 0}
    )
    await app.state.job_repo.append_event(
        waiting.id, "node_assigned", {"kind": "download", "node_id": "mac-mini"}
    )
    events = await app.state.job_repo.list_events(waiting.id)
    wait_event = next(e for e in events if e.event == "node_wait")
    await _backdate(
        session_factory, type(wait_event), wait_event.id,
        age_seconds=settings.node_download_wait_seconds + 86400,
    )
    listing = await c.get("/api/admin/jobs", headers=HEADERS)
    flags_by_id = {row["jobId"]: row["flags"] for row in listing.json()["jobs"]}
    assert "wait_over_window" not in flags_by_id[waiting.id.hex]
    assert "wait_over_window" not in flags_by_id[done[0].id.hex]


# --- direct positive/negative coverage for every discrepancy check -----------------


async def _flags_for_job(c, job):
    listing = await c.get("/api/admin/jobs", headers=HEADERS)
    return next(r["flags"] for r in listing.json()["jobs"] if r["jobId"] == job.id.hex)


async def test_busy_pointer_naming_closed_work_is_flagged_and_open_work_is_not(
    admin_client, session_factory, node_repo
):
    c, app = admin_client
    await node_repo.heartbeat("mac-mini", capabilities={"download": True})
    # Open assignment held by the node: healthy, no flag.
    healthy = await _make_job(
        app, source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c"
    )
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed.id == healthy.id
    live = await node_repo.create_assignment(
        healthy.id, worker_id="w", node_id="mac-mini",
        kind=NodeAssignmentKind.download, payload={},
    )
    assert live is not None
    assert "node_busy_pointer_stale" not in await _flags_for_job(c, healthy)
    # Free the node so the leak scenario can take it.
    assert await node_repo.complete(
        live.id, node_id="mac-mini", detail={"sha256": "a" * 64}
    )

    # Closed assignment still named by the busy pointer: flag.
    leaked = await _make_job(
        app, source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c", title="Leak"
    )
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed.id == leaked.id
    dead = await node_repo.create_assignment(
        leaked.id, worker_id="w", node_id="mac-mini",
        kind=NodeAssignmentKind.download, payload={},
    )
    assert dead is not None
    assert await node_repo.fail(
        dead.id, node_id="mac-mini", detail={"code": "DOWNLOAD_FAILED", "detail": "x"}
    )  # terminal report frees the node...
    async with session_factory() as session, session.begin():
        from shizzle_server.db.models import Node

        node = await session.get(Node, "mac-mini")
        node.busy_assignment_id = dead.id  # ...simulate the leaked pointer
    assert "node_busy_pointer_stale" in await _flags_for_job(c, leaked)


async def test_unresolved_reservation_flagged_only_past_the_b10_window(
    admin_client, session_factory
):
    c, app = admin_client
    settings: Settings = app.state.settings
    window = (
        max(settings.runpod_queue_timeout_seconds, settings.runpod_cold_start_seconds)
        + settings.runpod_worker_stall_seconds
    )
    job = await _make_job(app, title="Reserved")
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed.id == job.id
    await app.state.job_repo.advance(
        job.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await app.state.job_repo.advance(
        job.id, worker_id="w", from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    assert await app.state.job_repo.reserve_dispatch(
        job.id, worker_id="w", idempotency_key=f"{job.id.hex}:0"
    )
    # Fresh reservation: within the reconciliation window, no flag.
    assert "unresolved_dispatch_reservation" not in await _flags_for_job(c, job)
    reservation = next(
        e
        for e in await app.state.job_repo.list_events(job.id)
        if e.event == "runpod_dispatch_started"
    )
    await _backdate(
        session_factory, type(reservation), reservation.id, age_seconds=window + 600
    )
    assert "unresolved_dispatch_reservation" in await _flags_for_job(c, job)


async def test_stage_stall_flagged_only_when_the_stage_is_silent(
    admin_client, session_factory
):
    c, app = admin_client
    settings: Settings = app.state.settings
    silent = await _make_job(app, title="Silent")
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed.id == silent.id
    await app.state.job_repo.advance(
        silent.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await app.state.job_repo.advance(
        silent.id, worker_id="w", from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    entry = [
        e
        for e in await app.state.job_repo.list_events(silent.id)
        if e.event == "stage_completed" and e.detail.get("to") == "dispatched"
    ][-1]
    # The dispatched stage (RunPod executor) goes quiet past
    # max(queue, cold) + stall + slack with no meaningful event after entry.
    bound = (
        max(settings.runpod_queue_timeout_seconds, settings.runpod_cold_start_seconds)
        + settings.runpod_worker_stall_seconds
        + 600
    )
    await _backdate(session_factory, type(entry), entry.id, age_seconds=bound + 3600)
    assert "stage_stalled" in await _flags_for_job(c, silent)

    # A busy stage (recent events) is not stalled.
    busy = await _make_job(app, title="Busy")
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed.id == busy.id
    await app.state.job_repo.advance(
        busy.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    assert "stage_stalled" not in await _flags_for_job(c, busy)


async def test_healthy_pipeline_rows_carry_no_flags(admin_client, session_factory):
    """The negative column: a fully published job and a freshly claimed job
    produce zero discrepancy flags."""
    c, app = admin_client
    done, _track = await _published_job(app, session_factory)
    fresh = await _make_job(app, title="Fresh")
    await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    listing = await c.get("/api/admin/jobs", headers=HEADERS)
    flags_by_id = {row["jobId"]: row["flags"] for row in listing.json()["jobs"]}
    assert flags_by_id[done.id.hex] == []
    assert flags_by_id[fresh.id.hex] == []


async def test_open_assignment_on_runnable_job_is_not_terminal_leak(admin_client, node_repo):
    """Negative for open_assignment_on_terminal_job: an open assignment on a
    LIVE job is normal, and neither ack-overdue nor stalled while fresh."""
    c, app = admin_client
    await node_repo.heartbeat("mac-mini", capabilities={"download": True})
    job = await _make_job(
        app, source_type=SourceType.url, source_ref="https://youtu.be/x1y2z3a4b5c"
    )
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed.id == job.id
    assignment = await node_repo.create_assignment(
        job.id, worker_id="w", node_id="mac-mini",
        kind=NodeAssignmentKind.download, payload={},
    )
    assert assignment is not None
    flags = await _flags_for_job(c, job)
    assert "open_assignment_on_terminal_job" not in flags
    assert "assignment_ack_overdue" not in flags
    assert "assignment_stalled" not in flags
    assert "assignment_over_cap" not in flags


# --- top-level diagnostic fields are redacted (G2) -------------------------------


async def test_failed_job_error_detail_is_redacted_everywhere(admin_client):
    """jobs.error_detail is a diagnostic string the pipeline fills with
    upstream text (node errors quote URLs). Neither the list rows nor the
    detail columns may echo it raw."""
    c, app = admin_client
    job = await _make_job(app, source_type=SourceType.url, source_ref=CRED_URL, title="Boom")
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed.id == job.id
    await app.state.job_repo.advance(
        job.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await app.state.job_repo.fail_job(
        job.id,
        error_code=ErrorCode.NODE_FAILED,
        error_detail=f"node mac-mini download refused {CRED_URL} (token expired)",
        worker_id="w",
    )
    listing = await c.get("/api/admin/jobs?status=failed", headers=HEADERS)
    row = listing.json()["jobs"][0]
    assert row["errorCode"] == "NODE_FAILED"
    assert CRED_FRAGMENT not in row["errorDetail"]
    assert "[redacted]" in row["errorDetail"]

    detail = await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)
    body = detail.json()
    assert CRED_FRAGMENT not in detail.text
    assert "https://" not in detail.text.replace("http://test", "")
    assert "[redacted]" in body["job"]["errorDetail"]


# --- dispatch identity survives later-stage retries (durable history) -------------


async def _dispatched_selected_job(app, *, fail_after: JobStage | None):
    """Attempt 0 confirmed, its package selected at verifying; optionally a
    retry at ``fail_after`` increments jobs.attempt WITHOUT redispatching."""
    import hashlib

    job = await _make_job(app, title="Selected")
    assert (await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)).id == job.id
    key0 = f"{job.id.hex}:0"
    for from_stage, to_stage in (
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.dispatched),
    ):
        await app.state.job_repo.advance(
            job.id, worker_id="w", from_stage=from_stage, to_stage=to_stage
        )
    assert await app.state.job_repo.reserve_dispatch(job.id, worker_id="w", idempotency_key=key0)
    await app.state.job_repo.confirm_dispatch(
        job.id, idempotency_key=key0, runpod_job_id="rp-1"
    )
    prefix = (
        f"tracks/{track_id_for_job(job.id)}/1/separation/attempts/"
        f"{hashlib.sha256(key0.encode()).hexdigest()}"
    )
    await app.state.job_repo.advance(
        job.id, worker_id="w", from_stage=JobStage.dispatched, to_stage=JobStage.verifying,
        detail={"package_prefix": prefix},
    )
    if fail_after is not None:
        await app.state.job_repo.advance(
            job.id, worker_id="w", from_stage=JobStage.verifying, to_stage=JobStage.publishing
        )
    await app.state.job_repo.schedule_retry(
        job.id,
        worker_id="w",
        error_code=ErrorCode.CHECKSUM_MISMATCH,
        error_detail="retry without redispatch",
        retry_in_seconds=60.0,
    )
    retried = await app.state.job_repo.get_job(job.id)
    assert retried.attempt == 1  # the retry counter moved...
    assert retried.runpod_job_id == "rp-1"  # ...without any new dispatch
    return job, key0, prefix


async def test_verification_retry_keeps_the_selected_dispatch_identity(admin_client):
    """A verification retry bumps jobs.attempt but the job's identity is still
    attempt 0's dispatch; the evidence compares consistent keys."""
    c, app = admin_client
    job, key0, prefix = await _dispatched_selected_job(app, fail_after=None)
    detail = await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)
    body = detail.json()
    assert body["dispatchAttempt"]["idempotencyKey"] == key0  # NOT <hex>:1
    assert body["dispatchAttempt"]["attemptPrefix"] == prefix
    assert body["job"]["attempt"] == 1  # the retry counter stays separate
    identity = next(i for i in body["evidence"] if i["name"] == "dispatch_identity")
    assert identity["status"] == "ok"
    assert f"key={key0}" in identity["observed"]
    assert "retry counter now at" in identity["expected"]


async def test_publication_retry_keeps_the_selected_dispatch_identity(admin_client):
    c, app = admin_client
    job, key0, prefix = await _dispatched_selected_job(app, fail_after=JobStage.publishing)
    detail = await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)
    body = detail.json()
    assert body["dispatchAttempt"]["idempotencyKey"] == key0
    assert body["dispatchAttempt"]["attemptPrefix"] == prefix
    assert body["job"]["attempt"] == 1
    identity = next(i for i in body["evidence"] if i["name"] == "dispatch_identity")
    assert identity["status"] == "ok"


async def test_contradictory_confirmed_dispatch_is_a_mismatch(admin_client):
    """When the LAST confirmation names a different key than the selected
    dispatch (and no worker is marked failed), the identities contradict and
    the evidence says so instead of ok."""
    c, app = admin_client
    job, key0, _prefix = await _dispatched_selected_job(app, fail_after=None)
    await app.state.job_repo.append_event(
        job.id,
        "runpod_dispatched",
        {"runpod_job_id": "rp-foreign", "idempotency_key": f"{job.id.hex}:9"},
    )
    detail = await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)
    body = detail.json()
    identity = next(i for i in body["evidence"] if i["name"] == "dispatch_identity")
    assert identity["status"] == "mismatch"
    assert f"key={job.id.hex}:9" in identity["observed"]
    assert f"selected dispatch {key0}" in identity["observed"]


# --- every admin response crosses the redaction boundary (G6) ---------------------


async def test_every_admin_response_string_uses_the_redaction_choke_point(
    admin_client, session_factory, monkeypatch
):
    """Poison every free-text store, then walk every admin endpoint.

    A synthetic route with a brand-new unredacted field also proves that a
    future serializer cannot bypass the ASGI response boundary (G6).
    """
    from shizzle_server.api import admin as admin_module
    from shizzle_server.db.models import Track
    from shizzle_server.orchestrator.runtime import (
        NODE_ROUTING_KEY,
        RUNPOD_MAX_CONCURRENT_KEY,
    )

    c, app = admin_client
    job = await _make_job(
        app,
        source_type=SourceType.url,
        source_ref=CRED_URL,
        title=f"title {CRED_URL}",
        artist=f"artist {CRED_URL}",
    )
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed.id == job.id
    await app.state.job_repo.advance(
        job.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await app.state.node_repo.heartbeat(
        "mac-mini",
        display_name=f"node {CRED_URL}",
        capabilities={
            "device": f"device {CRED_URL}",
            "nested": {"detail": f"capability {CRED_URL}"},
        },
        agent_version=f"agent {CRED_URL}",
    )
    assignment = await app.state.node_repo.create_assignment(
        job.id,
        worker_id="w",
        node_id="mac-mini",
        kind=NodeAssignmentKind.download,
        payload={"url": CRED_URL},
        idempotency_key=f"assignment {CRED_URL}",
    )
    assert assignment is not None
    assert await app.state.node_repo.progress(
        assignment.id, node_id="mac-mini", phase=f"fetching {CRED_URL}"
    )
    await app.state.job_repo.append_event(
        job.id,
        "diagnostic",
        {"detail": f"event {CRED_URL}", "nested": [f"history {CRED_URL}"]},
    )

    track_id = track_id_for_job(job.id)
    async with session_factory() as session, session.begin():
        row = await session.get(Job, job.id)
        row.status = JobStage.ready
        row.title = f"title {CRED_URL}"
        row.artist = f"artist {CRED_URL}"
        row.runpod_job_id = CRED_URL
        row.executor = f"executor {CRED_URL}"
        row.worker_phase = f"phase {CRED_URL}"
        row.idempotency_key = CRED_URL
        row.lease_owner = f"lease {CRED_URL}"
        row.input_checksum = CRED_URL
        row.output_checksums = {"detail": f"output {CRED_URL}"}
        row.error_code = CRED_URL
        row.error_detail = f"failure {CRED_URL}"
        row.stage_timings = {"detail": f"timing {CRED_URL}"}
        row.track_id = track_id
        assignment_row = await session.get(type(assignment), assignment.id)
        assignment_row.detail = {"detail": f"assignment {CRED_URL}"}
        session.add(
            Track(
                id=track_id,
                title=f"track {CRED_URL}",
                artist=f"performer {CRED_URL}",
                duration_seconds=30.0,
                s3_prefix=f"tracks/{track_id}/1/{CRED_URL}",
                manifest_key=f"tracks/{track_id}/1/{CRED_URL}/manifest.json",
                generation=1,
                integrity={"detail": f"integrity {CRED_URL}"},
            )
        )
    await app.state.runtime_repo.set_value(NODE_ROUTING_KEY, CRED_URL)
    await app.state.runtime_repo.set_value(RUNPOD_MAX_CONCURRENT_KEY, CRED_URL)

    def _credential_error(_settings, _key):
        return admin_module._HeadOutcome(None, f"storage failed for {CRED_URL}")

    monkeypatch.setattr(admin_module, "_head_blocking", _credential_error)

    @app.get("/api/admin/_redaction_probe")
    async def _new_unredacted_response_field():
        return {
            "newFreeText": f"new field {CRED_URL}",
            "nested": [{"detail": f"future value {CRED_URL}"}],
        }

    calls = (
        ("jobs", c.get("/api/admin/jobs", headers=HEADERS)),
        ("job detail", c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)),
        (
            "job storage detail",
            c.get(f"/api/admin/jobs/{job.id}?verify=s3", headers=HEADERS),
        ),
        ("nodes", c.get("/api/admin/nodes", headers=HEADERS)),
        ("settings", c.get("/api/admin/settings", headers=HEADERS)),
        (
            "settings validation",
            c.put("/api/admin/settings", json={"nodeRouting": CRED_URL}, headers=HEADERS),
        ),
        (
            "settings update",
            c.put("/api/admin/settings", json={"nodeRouting": "prefer"}, headers=HEADERS),
        ),
        ("disable", c.post("/api/admin/nodes/mac-mini/disable", headers=HEADERS)),
        ("enable", c.post("/api/admin/nodes/mac-mini/enable", headers=HEADERS)),
        (
            "release",
            c.post(f"/api/admin/assignments/{assignment.id}/release", headers=HEADERS),
        ),
        ("future field", c.get("/api/admin/_redaction_probe", headers=HEADERS)),
    )
    for label, call in calls:
        response = await call
        assert response.status_code in (200, 422), label
        assert CRED_FRAGMENT not in response.text, label
        assert "https://" not in response.text, label


# --- handoff recovery selects the recovered reservation's identity ----------------


async def _recover_reserved_handoff(
    app, monkeypatch, *, retry: bool, package_key: str | None = None
):
    """Use real repository transitions and handle_dispatched() recovery.

    Only the external receipt/package observations are mocked. With ``retry``,
    attempt 0 is confirmed and failed before attempt 1 is reserved/recovered.
    """
    from shizzle_server.orchestrator import cloud
    from shizzle_server.orchestrator.runpod_client import NotConfiguredRunPodClient
    from shizzle_server.orchestrator.stages import StageContext, handle_dispatched

    job = await _make_job(app, title="Recover handoff")
    worker = "recovery-0"
    claimed = await app.state.job_repo.claim_next(worker_id=worker, lease_seconds=600)
    assert claimed.id == job.id
    for from_stage, to_stage in (
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.dispatched),
    ):
        await app.state.job_repo.advance(
            job.id, worker_id=worker, from_stage=from_stage, to_stage=to_stage
        )
    key0 = f"{job.id.hex}:0"
    assert await app.state.job_repo.reserve_dispatch(
        job.id, worker_id=worker, idempotency_key=key0
    )
    selected_key = key0
    if retry:
        await app.state.job_repo.confirm_dispatch(
            job.id, idempotency_key=key0, runpod_job_id="rp-dead"
        )
        await app.state.job_repo.record_worker_progress(
            job.id, phase="failed", worker_id=worker
        )
        await app.state.job_repo.schedule_retry(
            job.id,
            worker_id=worker,
            error_code=ErrorCode.RUNPOD_DISPATCH_FAILED,
            error_detail="attempt 0 failed",
            retry_in_seconds=0,
        )
        worker = "recovery-1"
        claimed = await app.state.job_repo.claim_next(worker_id=worker, lease_seconds=600)
        assert claimed.id == job.id
        job = claimed
        selected_key = f"{job.id.hex}:1"
        assert await app.state.job_repo.reserve_dispatch(
            job.id, worker_id=worker, idempotency_key=selected_key
        )
    else:
        job = claimed

    async def _no_receipt(_ctx, *, idempotency_key):
        assert idempotency_key == selected_key
        return None

    async def _handoff_present(_ctx, *, idempotency_key):
        assert idempotency_key == selected_key
        return True

    monkeypatch.setattr(cloud, "accepted_dispatch_id", _no_receipt)
    monkeypatch.setattr(cloud, "package_ready", _handoff_present)
    ctx = StageContext(
        job=job,
        settings=app.state.settings,
        pipeline=None,  # type: ignore[arg-type]
        jobs=app.state.job_repo,
        runpod=NotConfiguredRunPodClient(),
        worker_id=worker,
    )
    assert await handle_dispatched(ctx) == JobStage.verifying
    if package_key is not None:
        ctx.detail["package_prefix"] = cloud.attempt_prefix(
            track_id_for_job(job.id), package_key
        )
    await app.state.job_repo.advance(
        job.id,
        worker_id=worker,
        from_stage=JobStage.dispatched,
        to_stage=JobStage.verifying,
        detail=ctx.detail,
    )
    return job, selected_key, cloud.attempt_prefix(track_id_for_job(job.id), selected_key)


async def test_fresh_handoff_recovery_reports_the_reservation_identity(
    admin_client, monkeypatch
):
    c, app = admin_client
    job, key, prefix = await _recover_reserved_handoff(app, monkeypatch, retry=False)
    body = (await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)).json()
    assert body["dispatchAttempt"]["idempotencyKey"] == key
    assert body["dispatchAttempt"]["attemptPrefix"] == prefix
    identity = next(i for i in body["evidence"] if i["name"] == "dispatch_identity")
    assert identity["status"] == "ok"
    assert f"recovered from handoff.json key={key}" in identity["observed"]


async def test_retry_handoff_recovery_overrides_the_dead_confirmation(
    admin_client, monkeypatch
):
    c, app = admin_client
    job, key1, prefix1 = await _recover_reserved_handoff(app, monkeypatch, retry=True)
    body = (await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)).json()
    assert body["dispatchAttempt"]["idempotencyKey"] == key1
    assert body["dispatchAttempt"]["idempotencyKey"] != f"{job.id.hex}:0"
    assert body["dispatchAttempt"]["attemptPrefix"] == prefix1
    identity = next(i for i in body["evidence"] if i["name"] == "dispatch_identity")
    assert identity["status"] == "ok"
    assert f"recovered from handoff.json key={key1}" in identity["observed"]


async def test_recovered_identity_must_match_the_selected_package_prefix(
    admin_client, monkeypatch
):
    c, app = admin_client
    job, key1, _prefix1 = await _recover_reserved_handoff(
        app, monkeypatch, retry=True, package_key="wrong-attempt"
    )
    body = (await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)).json()
    assert body["dispatchAttempt"]["idempotencyKey"] == key1
    identity = next(i for i in body["evidence"] if i["name"] == "dispatch_identity")
    assert identity["status"] == "mismatch"
    assert "selected package prefix" in identity["observed"]
    assert "belongs to another key" in identity["observed"]


async def test_redaction_covers_url_keys_mixed_case_schemes_and_collisions(
    admin_client,
):
    """URL-bearing keys are response text too; collisions must not drop data."""
    c, app = admin_client
    job = await _make_job(app, source_type=SourceType.url, source_ref=CRED_URL)
    claimed = await app.state.job_repo.claim_next(worker_id="w", lease_seconds=600)
    assert claimed.id == job.id
    await app.state.job_repo.advance(
        job.id, worker_id="w", from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    event_key = "event HTTPS://example.test/?token=event-key-secret"
    await app.state.job_repo.append_event(
        job.id,
        "diagnostic",
        {event_key: "value HTTP://example.test/?token=event-value-secret"},
    )
    await app.state.node_repo.heartbeat(
        "mac-mini",
        capabilities={
            "HTTPS://example.test/?token=capability-one-secret": "first",
            "[redacted]": "pre-existing collision",
            "HTTP://example.test/?token=capability-two-secret": "second",
        },
    )
    assignment = await app.state.node_repo.create_assignment(
        job.id,
        worker_id="w",
        node_id="mac-mini",
        kind=NodeAssignmentKind.download,
        payload={"url": CRED_URL},
    )
    assert assignment is not None
    assert await app.state.node_repo.progress(
        assignment.id,
        node_id="mac-mini",
        phase="fetching HTTPS://example.test/?token=phase-secret",
    )

    detail = await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)
    nodes = await c.get("/api/admin/nodes", headers=HEADERS)
    for response in (detail, nodes):
        assert response.status_code == 200
        assert "http://" not in response.text.lower()
        for secret in (
            "event-key-secret",
            "event-value-secret",
            "capability-one-secret",
            "capability-two-secret",
            "phase-secret",
        ):
            assert secret not in response.text

    event = next(e for e in detail.json()["events"] if e["event"] == "diagnostic")
    assert event["detail"] == {"event [redacted]": "[redacted]"}
    current = nodes.json()["nodes"][0]["currentAssignment"]
    assert current["phase"] == "fetching [redacted]"
    capabilities = nodes.json()["nodes"][0]["capabilities"]
    assert len(capabilities) == 3  # both sanitized collisions were retained
    assert list(capabilities.values()).count("[redacted]") == 2
    assert "pre-existing collision" in capabilities.values()
    assert all("token" not in key.lower() for key in capabilities)


async def test_outstanding_retry_reservation_supersedes_closed_confirmation(
    admin_client,
):
    """Attempt 0 is closed; reserved attempt 1 is already the current identity."""
    from shizzle_server.orchestrator import cloud

    c, app = admin_client
    job = await _make_job(app, title="Reserved retry")
    claimed = await app.state.job_repo.claim_next(worker_id="w0", lease_seconds=600)
    assert claimed.id == job.id
    for from_stage, to_stage in (
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.dispatched),
    ):
        await app.state.job_repo.advance(
            job.id, worker_id="w0", from_stage=from_stage, to_stage=to_stage
        )
    key0 = f"{job.id.hex}:0"
    assert await app.state.job_repo.reserve_dispatch(
        job.id, worker_id="w0", idempotency_key=key0
    )
    await app.state.job_repo.confirm_dispatch(
        job.id, idempotency_key=key0, runpod_job_id="rp-dead"
    )
    await app.state.job_repo.record_worker_progress(
        job.id, phase="failed", worker_id="w0"
    )
    await app.state.job_repo.schedule_retry(
        job.id,
        worker_id="w0",
        error_code=ErrorCode.RUNPOD_DISPATCH_FAILED,
        error_detail="attempt 0 failed",
        retry_in_seconds=0,
    )
    claimed = await app.state.job_repo.claim_next(worker_id="w1", lease_seconds=600)
    assert claimed.id == job.id and claimed.attempt == 1
    key1 = f"{job.id.hex}:1"
    assert await app.state.job_repo.reserve_dispatch(
        job.id, worker_id="w1", idempotency_key=key1
    )

    body = (await c.get(f"/api/admin/jobs/{job.id}", headers=HEADERS)).json()
    assert body["dispatchAttempt"]["idempotencyKey"] == key1
    assert body["dispatchAttempt"]["attemptPrefix"] == cloud.attempt_prefix(
        track_id_for_job(job.id), key1
    )
    identity = next(i for i in body["evidence"] if i["name"] == "dispatch_identity")
    assert identity["status"] == "ok"
    assert identity["observed"] == f"reservation key={key1} unconfirmed"

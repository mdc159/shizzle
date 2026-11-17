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

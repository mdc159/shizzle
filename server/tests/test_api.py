"""Unit: DB-backed API endpoints via in-process ASGI transport (SQLite)."""

from __future__ import annotations

import json

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from shizzle_server.db.models import JobStage
from shizzle_server.main import create_app


@pytest_asyncio.fixture
async def client(settings):
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, app


async def test_upload_creates_job_row(client):
    c, app = client
    resp = await c.post(
        "/api/upload",
        files={"file": ("my song.mp4", b"fake mp4 bytes", "video/mp4")},
    )
    assert resp.status_code == 200
    job_id = resp.json()["jobId"]

    status = await c.get(f"/api/jobs/{job_id}")
    assert status.status_code == 200
    body = status.json()
    assert body["status"] == "pending"
    assert body["sourceType"] == "upload"
    assert body["attempt"] == 0

    # Source file landed on disk under the job dir
    settings = app.state.settings
    assert (settings.data_dir / job_id / "source.mp4").read_bytes() == b"fake mp4 bytes"

    # Job row has provenance: created event + input checksum
    import uuid as uuid_mod

    job = await app.state.job_repo.get_job(uuid_mod.UUID(job_id))
    assert job.input_checksum is not None and len(job.input_checksum) == 64


async def test_upload_rejects_non_video(client):
    c, _ = client
    resp = await c.post(
        "/api/upload", files={"file": ("notes.txt", b"hello", "text/plain")}
    )
    assert resp.status_code == 400


async def test_submit_url_creates_job(client):
    c, _ = client
    resp = await c.post("/api/submit-url", json={"url": "https://youtube.com/watch?v=x"})
    assert resp.status_code == 200
    job_id = resp.json()["jobId"]
    status = (await c.get(f"/api/jobs/{job_id}")).json()
    assert status["sourceType"] == "url"

    bad = await c.post("/api/submit-url", json={"url": "not a url"})
    assert bad.status_code == 400


async def test_job_history_pagination_newest_first(client):
    c, _ = client
    for i in range(3):
        await c.post("/api/submit-url", json={"url": f"https://youtube.com/{i}"})
    resp = await c.get("/api/jobs", params={"limit": 2, "offset": 0})
    body = resp.json()
    assert body["total"] == 3
    assert len(body["jobs"]) == 2
    times = [j["createdAt"] for j in body["jobs"]]
    assert times == sorted(times, reverse=True)


async def test_job_not_found(client):
    c, _ = client
    assert (await c.get("/api/jobs/nope")).status_code == 404
    assert (await c.get("/api/jobs/00000000000000000000000000000000")).status_code == 404


async def test_library_track_serving_and_soft_delete(client, upload_job):
    c, app = client
    repo = app.state.job_repo
    settings = app.state.settings

    # Drive the fixture job to published state
    for a, b in [
        (JobStage.pending, JobStage.downloading),
        (JobStage.downloading, JobStage.splitting),
        (JobStage.splitting, JobStage.verifying),
        (JobStage.verifying, JobStage.publishing),
    ]:
        await repo.advance(upload_job.id, from_stage=a, to_stage=b)
    track = await repo.publish_track(
        upload_job.id,
        title="Golden",
        duration_seconds=30.0,
        s3_prefix=f"local/{upload_job.id.hex}",
        manifest_key=f"local/{upload_job.id.hex}/stems.json",
    )
    job_dir = settings.data_dir / upload_job.id.hex
    (job_dir / "stems.json").write_text(json.dumps({"version": 3, "title": "Golden"}))

    # Library lists the track with a track-served publicUrl
    lib = (await c.get("/api/library")).json()
    assert lib["total"] == 1
    entry = lib["tracks"][0]
    assert entry["title"] == "Golden"
    assert entry["publicUrl"] == f"/api/tracks/{track.id}"

    # Manifest serving through the track route
    manifest = await c.get(f"/api/tracks/{track.id}/stems.json")
    assert manifest.status_code == 200
    assert manifest.json()["title"] == "Golden"

    # Traversal guard carried over
    evil = await c.get(f"/api/tracks/{track.id}/..%2f..%2fsecrets.txt")
    assert evil.status_code in (403, 404)

    # Soft delete removes it from the library but keeps the row
    deleted = await c.delete(f"/api/tracks/{track.id}")
    assert deleted.status_code == 200
    assert (await c.get("/api/library")).json()["total"] == 0
    assert (await c.get(f"/api/tracks/{track.id}/stems.json")).status_code == 404
    assert (await c.delete(f"/api/tracks/{track.id}")).status_code == 404


async def test_health_reports_db_and_orchestrator(client):
    c, app = client
    resp = await c.get("/api/health")
    body = resp.json()
    assert body["api"] is True
    assert body["db"] is True
    # No orchestrator heartbeat yet -> degraded but honest
    assert body["orchestratorAlive"] is False
    assert body["status"] == "degraded"

    # After a heartbeat it goes green
    await app.state.heartbeat_repo.beat("test-worker")
    body = (await c.get("/api/health")).json()
    assert body["orchestratorAlive"] is True
    assert body["status"] == "ok"

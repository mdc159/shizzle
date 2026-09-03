"""Unit: DB-backed API endpoints via in-process ASGI transport (SQLite)."""

from __future__ import annotations

import json
import uuid

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from shizzle_server.db.models import JobStage
from shizzle_server.db.repository import track_id_for_job
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
    assert body["title"] == "my song"
    assert body["attempt"] == 0
    assert body["workerPhase"] is None
    assert body["workerHeartbeatAt"] is None

    # Source file landed on disk under the job dir
    settings = app.state.settings
    assert (settings.data_dir / job_id / "source.mp4").read_bytes() == b"fake mp4 bytes"

    # Job row has provenance: created event + input checksum
    import uuid as uuid_mod

    job = await app.state.job_repo.get_job(uuid_mod.UUID(job_id))
    assert job.input_checksum is not None and len(job.input_checksum) == 64


async def test_upload_rejects_non_video(client):
    c, _ = client
    resp = await c.post("/api/upload", files={"file": ("notes.txt", b"hello", "text/plain")})
    assert resp.status_code == 400


async def test_upload_parses_artist_and_cleans_title_from_filename(client):
    c, app = client
    resp = await c.post(
        "/api/upload",
        files={
            "file": (
                "Van Halen - Runnin' With The Devil (Official Music Video).mp4",
                b"fake mp4 bytes",
                "video/mp4",
            )
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Runnin' With The Devil"
    assert body["artist"] == "Van Halen"

    status = (await c.get(f"/api/jobs/{body['jobId']}")).json()
    assert status["title"] == "Runnin' With The Devil"
    assert status["artist"] == "Van Halen"


async def test_upload_explicit_title_and_artist_win(client):
    c, _ = client
    resp = await c.post(
        "/api/upload",
        files={"file": ("raw name.mp4", b"fake mp4 bytes", "video/mp4")},
        data={"title": "Black Hole Sun (Guitar Center Sessions)", "artist": "Peter Frampton"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Black Hole Sun (Guitar Center Sessions)"
    assert body["artist"] == "Peter Frampton"


async def test_upload_title_without_artist_is_parsed(client):
    c, _ = client
    resp = await c.post(
        "/api/upload",
        files={"file": ("raw name.mp4", b"fake mp4 bytes", "video/mp4")},
        data={"title": "Tool - The Pot (Official Video)"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "The Pot"
    assert body["artist"] == "Tool"


async def test_upload_typed_artist_overrides_filename(client):
    c, _ = client
    resp = await c.post(
        "/api/upload",
        files={"file": ("sg - spoonman.mp4", b"fake mp4 bytes", "video/mp4")},
        data={"artist": "Soundgarden"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "spoonman"
    assert body["artist"] == "Soundgarden"


async def test_upload_bounds_title_and_artist_length(client):
    """Metadata fields are bounded so they cannot bypass the size guard."""
    c, _ = client
    resp = await c.post(
        "/api/upload",
        files={"file": ("x.mp4", b"fake mp4 bytes", "video/mp4")},
        data={"title": "t" * 1025, "artist": "a" * 1025},
    )
    assert resp.status_code == 422
    ok = await c.post(
        "/api/upload",
        files={"file": ("x.mp4", b"fake mp4 bytes", "video/mp4")},
        data={"title": "t" * 1024, "artist": "a" * 1024},
    )
    assert ok.status_code == 200


async def test_submit_url_bounds_title_and_artist_length(client):
    c, _ = client
    resp = await c.post(
        "/api/submit-url",
        json={"url": "https://youtube.com/watch?v=x", "title": "t" * 1025},
    )
    assert resp.status_code == 422


async def test_submit_url_creates_job(client):
    c, _ = client
    resp = await c.post("/api/submit-url", json={"url": "https://youtube.com/watch?v=x"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] is None
    assert body["artist"] == ""
    job_id = body["jobId"]
    status = (await c.get(f"/api/jobs/{job_id}")).json()
    assert status["sourceType"] == "url"

    bad = await c.post("/api/submit-url", json={"url": "not a url"})
    assert bad.status_code == 400


async def test_submit_url_parses_title_and_accepts_artist(client):
    c, _ = client
    resp = await c.post(
        "/api/submit-url",
        json={
            "url": "https://youtube.com/watch?v=x",
            "title": "AC DC - Let There Be Rock (Official Video)",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Let There Be Rock"
    assert body["artist"] == "AC DC"

    resp = await c.post(
        "/api/submit-url",
        json={"url": "https://youtube.com/watch?v=y", "title": "The Pot", "artist": "TOOL"},
    )
    body = resp.json()
    assert body["title"] == "The Pot"
    assert body["artist"] == "TOOL"  # typed artist wins; no re-casing


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
    assert (await c.get("/api/jobs/nope/events")).status_code == 404
    assert (
        await c.get("/api/jobs/00000000000000000000000000000000/events")
    ).status_code == 404


async def test_job_events_and_worker_fields(client):
    c, app = client
    job_id = (await c.post(
        "/api/submit-url",
        json={"url": "https://example.com/song", "title": "Timeline"},
    )).json()["jobId"]
    parsed_id = uuid.UUID(job_id)
    await app.state.job_repo.advance(
        parsed_id, from_stage=JobStage.pending, to_stage=JobStage.downloading
    )
    await app.state.job_repo.advance(
        parsed_id, from_stage=JobStage.downloading, to_stage=JobStage.dispatched
    )
    await app.state.job_repo.claim_next(worker_id="api-test", lease_seconds=60)
    await app.state.job_repo.record_dispatch(
        parsed_id, worker_id="api-test", runpod_job_id="runpod-1"
    )
    await app.state.job_repo.record_worker_progress(parsed_id, phase="separating")

    status = (await c.get(f"/api/jobs/{job_id}")).json()
    assert status["title"] == "Timeline"
    assert status["workerPhase"] == "separating"
    assert status["workerHeartbeatAt"] is not None

    response = await c.get(f"/api/jobs/{job_id}/events")
    assert response.status_code == 200
    events = response.json()["events"]
    assert [event["event"] for event in events] == [
        "created",
        "stage_completed",
        "stage_completed",
        "runpod_dispatched",
        "worker_progress",
    ]
    assert events[-1]["detail"] == {"phase": "separating"}
    assert all(event["createdAt"] for event in events)


async def test_library_track_serving_and_soft_delete(client, upload_job, session_factory):
    from shizzle_server.db.models import Track

    c, app = client
    settings = app.state.settings

    # A local/ placeholder row (the pre-C7 shape). publish_track now refuses
    # this shape, so insert the row directly to exercise the serving routes.
    track_id = track_id_for_job(upload_job.id)
    async with session_factory() as session, session.begin():
        session.add(
            Track(
                id=track_id,
                title="Golden",
                duration_seconds=30.0,
                s3_prefix=f"local/{upload_job.id.hex}",
                manifest_key=f"local/{upload_job.id.hex}/stems.json",
            )
        )
    job_dir = settings.data_dir / upload_job.id.hex
    (job_dir / "stems.json").write_text(json.dumps({"version": 3, "title": "Golden"}))

    # C7 defensive filter: the library never lists a row outside tracks/.
    lib = (await c.get("/api/library")).json()
    assert lib["total"] == 0

    # Direct-URL local serving still works for the row that exists.
    manifest = await c.get(f"/api/tracks/{track_id}/stems.json")
    assert manifest.status_code == 200
    assert manifest.json()["title"] == "Golden"

    # Traversal guard carried over
    evil = await c.get(f"/api/tracks/{track_id}/..%2f..%2fsecrets.txt")
    assert evil.status_code in (403, 404)

    # Soft delete keeps the row but serving stops
    deleted = await c.delete(f"/api/tracks/{track_id}")
    assert deleted.status_code == 200
    assert (await c.get("/api/library")).json()["total"] == 0
    assert (await c.get(f"/api/tracks/{track_id}/stems.json")).status_code == 404
    assert (await c.delete(f"/api/tracks/{track_id}")).status_code == 404


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


async def test_playback_telemetry_route_persists_direct_evidence(client):
    from shizzle_server.db.repository import track_id_for_import

    c, app = client
    track_id = track_id_for_import("api-telemetry")
    await app.state.track_repo.upsert_imported(
        track_id,
        title="Telemetry",
        duration_seconds=30,
        s3_prefix=f"tracks/{track_id}/2",
        manifest_key=f"tracks/{track_id}/2/manifest.json",
        generation=2,
    )
    session_id = uuid.uuid4()
    payload = {
        "sessionId": str(session_id),
        "trackId": str(track_id),
        "generation": 2,
        "sequence": 1,
        "event": "stem-clock-stalled",
        "clientAtMs": 1234,
        "appBuild": "index-test.js",
        "browser": "test-browser",
        "detail": {"stems": {"bass": {"paused": True}}, "output": {"rmsDbfs": -20}},
    }
    response = await c.post("/api/playback/telemetry", json=payload)
    assert response.status_code == 200
    assert response.json() == {"accepted": True}
    duplicate = await c.post("/api/playback/telemetry", json=payload)
    assert duplicate.status_code == 200
    assert duplicate.json() == {"accepted": False}
    events = await app.state.playback_telemetry_repo.list_events(session_id)
    assert len(events) == 1
    assert events[0].event == "stem-clock-stalled"
    assert events[0].detail["stems"]["bass"]["paused"] is True

    incidents = await c.get("/api/playback/incidents?minutes=5")
    assert incidents.status_code == 200
    incident_body = incidents.json()
    assert incident_body["total"] == 1
    assert incident_body["incidents"][0]["trackId"] == str(track_id)
    assert incident_body["incidents"][0]["generation"] == 2
    assert incident_body["incidents"][0]["event"] == "stem-clock-stalled"

    forbidden = {**payload, "sequence": 2, "detail": {"accessToken": "secret"}}
    assert (await c.post("/api/playback/telemetry", json=forbidden)).status_code == 422

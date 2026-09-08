"""Completed imports preserve bytes and converge without a source pipeline."""

import base64
import copy
import hashlib
from datetime import timedelta

import boto3
import pytest
from httpx import ASGITransport, AsyncClient
from moto import mock_aws
from sqlalchemy import select

from shizzle_server.api import imports
from shizzle_server.api.auth import create_device_token
from shizzle_server.db.models import CompletedImport, CompletedImportEvent, Track, utcnow
from shizzle_server.main import create_app
from shizzle_server.publish import completed
from shizzle_server.publish.completed import ImportRepository, InvalidCandidate


@pytest.fixture
def manifest():
    objects = [
        {"file": f, "sha256": hashlib.sha256(f.encode()).hexdigest(), "bytes": len(f)}
        for f in completed.FILES
    ]
    return {
        "version": 3,
        "delivery_profile": "shizzle-browser-v1",
        "title": "Test",
        "artist": "Fixture",
        "duration": 10,
        "video": "video.mp4",
        "timeline": {"start_ms": 0, "duration_ms": 10000, "sample_rate_hz": 44100},
        "stems": [
            {"id": r, "name": r, "file": f"stems/{r}.m4a", "default_gain_db": -6}
            for r in completed.CANONICAL_STEM_IDS
        ],
        "integrity": {"objects": objects},
    }


@pytest.fixture
def storage(monkeypatch, settings):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=settings.s3_media_bucket)
        monkeypatch.setattr(completed, "s3_client", lambda _: s3)
        monkeypatch.setattr(imports, "s3_client", lambda _: s3)
        yield s3


def put_inputs(storage, settings, item):
    for obj in item.manifest["integrity"]["objects"]:
        storage.put_object(
            Bucket=settings.s3_media_bucket,
            Key=completed.input_key(item, obj["file"]),
            Body=obj["file"].encode(),
            ChecksumSHA256=base64.b64encode(bytes.fromhex(obj["sha256"])).decode(),
        )


async def test_publish_and_lost_response_converge(
    session_factory, settings, storage, manifest, monkeypatch
):
    repo = ImportRepository(session_factory)
    item = await repo.create(manifest)
    assert (await repo.create(copy.deepcopy(manifest))).id == item.id
    put_inputs(storage, settings, item)
    assert completed.upload_instructions(item, storage, settings.s3_media_bucket) == []
    await repo.finalize(item.id)
    monkeypatch.setattr(completed, "validate_candidate", lambda *_args: None)
    assert await completed.process_one(repo, settings, "first")
    assert (await repo.get(item.id)).status == "ready"
    assert not await completed.process_one(repo, settings, "second")
    assert (await repo.finalize(item.id)).status == "ready"
    tid = completed.track_id(item)
    async with session_factory() as session:
        assert len((await session.scalars(select(Track))).all()) == 1
        events = (
            await session.scalars(
                select(CompletedImportEvent.event).order_by(CompletedImportEvent.id)
            )
        ).all()
        assert events == ["uploading", "validating", "publishing", "ready"]
    for obj in manifest["integrity"]["objects"]:
        body = storage.get_object(
            Bucket=settings.s3_media_bucket, Key=f"tracks/{tid}/1/{obj['file']}"
        )["Body"].read()
        assert hashlib.sha256(body).hexdigest() == obj["sha256"]


@pytest.mark.parametrize(
    "change", ["path", "missing", "duplicate", "gain", "duration", "size", "hash"]
)
def test_invalid_manifest(manifest, change):
    if change == "path":
        manifest["video"] = "../../secret"
    if change == "missing":
        manifest["stems"].pop()
    if change == "duplicate":
        manifest["integrity"]["objects"][-1] = manifest["integrity"]["objects"][0]
    if change == "gain":
        manifest["stems"][0]["default_gain_db"] = 1
    if change == "duration":
        manifest["duration"] = float("nan")
    if change == "size":
        manifest["integrity"]["objects"][0]["bytes"] = 129 * 1024**2
    if change == "hash":
        manifest["integrity"]["objects"][0]["sha256"] = "invalid"
    with pytest.raises(InvalidCandidate):
        completed.validate_manifest(manifest)


async def test_corrupt_bytes_never_register(session_factory, settings, storage, manifest):
    repo = ImportRepository(session_factory)
    item = await repo.create(manifest)
    put_inputs(storage, settings, item)
    storage.put_object(
        Bucket=settings.s3_media_bucket, Key=completed.input_key(item, "video.mp4"), Body=b"corrupt"
    )
    await repo.finalize(item.id)
    await completed.process_one(repo, settings, "worker")
    assert (await repo.get(item.id)).error_code == "INTEGRITY_FAILED"
    async with session_factory() as session:
        assert (await session.scalars(select(Track))).first() is None


async def test_expired_claim_is_recovered_and_stale_failure_fenced(session_factory, manifest):
    repo = ImportRepository(session_factory)
    item = await repo.create(manifest)
    await repo.finalize(item.id)
    await repo.claim("old", -1)
    assert (await repo.claim("new", 30)).id == item.id
    await repo.failure(item.id, "old", "incorrect", False)
    current = await repo.get(item.id)
    assert current.lease_owner == "new" and current.error_code is None


async def test_publication_then_registration_failure_recovers(
    session_factory, settings, storage, manifest, monkeypatch
):
    repo = ImportRepository(session_factory)
    item = await repo.create(manifest)
    put_inputs(storage, settings, item)
    await repo.finalize(item.id)
    monkeypatch.setattr(completed, "validate_candidate", lambda *_args: None)
    original = completed.publish_candidate

    def publish_then_die(*args):
        original(*args)
        raise RuntimeError("crash after manifest")

    monkeypatch.setattr(completed, "publish_candidate", publish_then_die)
    await completed.process_one(repo, settings, "first")
    assert (await repo.get(item.id)).status == "validating"
    monkeypatch.setattr(completed, "publish_candidate", original)
    async with session_factory() as session, session.begin():
        row = await session.get(CompletedImport, item.id)
        row.next_retry_at = utcnow() - timedelta(seconds=1)
    await completed.process_one(repo, settings, "second")
    assert (await repo.get(item.id)).status == "ready"


@pytest.mark.parametrize("deleted,generation", [(True, 1), (False, 2)])
async def test_existing_track_protected(
    session_factory, settings, storage, manifest, monkeypatch, deleted, generation
):
    repo = ImportRepository(session_factory)
    item = await repo.create(manifest)
    put_inputs(storage, settings, item)
    tid = completed.track_id(item)
    async with session_factory() as session, session.begin():
        session.add(
            Track(
                id=tid,
                title="Keep",
                artist="Original",
                duration_seconds=10,
                s3_prefix=f"tracks/{tid}/{generation}",
                manifest_key=f"tracks/{tid}/{generation}/manifest.json",
                generation=generation,
                deleted_at=utcnow() if deleted else None,
            )
        )
    monkeypatch.setattr(completed, "validate_candidate", lambda *_args: None)
    await repo.finalize(item.id)
    await completed.process_one(repo, settings, "worker")
    assert (await repo.get(item.id)).error_code == "TRACK_CONFLICT"
    async with session_factory() as session:
        assert (await session.get(Track, tid)).title == "Keep"


async def test_api_auth_limits_and_retry(settings, manifest, storage):
    settings.shizzle_completed_imports_enabled = True
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client,
    ):
        assert (await client.post("/api/imports", json=manifest)).status_code == 503
        settings.shizzle_passcode = "fixture-only"
        assert (await client.post("/api/imports", json=manifest)).status_code == 401
        token, _ = create_device_token(settings)
        client.headers["Authorization"] = "Bearer " + token
        assert (
            await client.post("/api/imports", content=b"x" * (completed.MAX_MANIFEST_BYTES + 1))
        ).status_code == 413
        one = (await client.post("/api/imports", json=manifest)).json()
        two = (await client.post("/api/imports", json=manifest)).json()
        assert one["importId"] == two["importId"]
        uploads = (await client.post(f"/api/imports/{one['importId']}/uploads")).json()["uploads"]
        assert len(uploads) == 7
        assert all(
            "imports/" in u["url"] and "x-amz-checksum-sha256" in u["headers"] for u in uploads
        )


def test_actual_completed_media_is_decoded_and_measured(tmp_path, manifest):
    import shutil
    import subprocess
    from types import SimpleNamespace

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is required for actual media audit")
    stems = tmp_path / "stems"
    stems.mkdir()
    audio = stems / "vocals.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anoisesrc=color=white:amplitude=0.02:sample_rate=44100:duration=3",
            "-ac",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-movflags",
            "+faststart",
            str(audio),
        ],
        check=True,
    )
    for role in completed.CANONICAL_STEM_IDS:
        if role != "vocals":
            shutil.copyfile(audio, stems / f"{role}.m4a")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=320x180:r=30:d=3",
            "-an",
            "-c:v",
            "libx264",
            "-profile:v",
            "main",
            "-level:v",
            "3.1",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "60",
            "-movflags",
            "+faststart",
            str(tmp_path / "video.mp4"),
        ],
        check=True,
    )
    manifest["duration"] = 3
    manifest["timeline"]["duration_ms"] = 3000
    for obj in manifest["integrity"]["objects"]:
        data = (tmp_path / obj["file"]).read_bytes()
        obj.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    before = completed.canonical(manifest)
    assert completed.validate_manifest(manifest)
    result = completed.validate_candidate(SimpleNamespace(manifest=manifest), tmp_path)
    assert result["default_mix"]["passed_pre_limiter"]
    assert all(a["full_decode"] == "pass" for a in result["objects"])
    assert completed.canonical(manifest) == before
    for obj in manifest["integrity"]["objects"]:
        assert hashlib.sha256((tmp_path / obj["file"]).read_bytes()).hexdigest() == obj["sha256"]


async def test_catalog_metadata_overrides_stored_manifest(settings, manifest, monkeypatch):
    from shizzle_server.api import media
    from shizzle_server.db import create_session_factory
    from shizzle_server.db.repository import track_id_for_import

    identifier = track_id_for_import("metadata-fixture")
    app = create_app(settings)
    monkeypatch.setattr(media, "load_s3_manifest", lambda *_args: copy.deepcopy(manifest))
    async with app.router.lifespan_context(app):
        sf = create_session_factory(app.state.engine)
        async with sf() as session, session.begin():
            session.add(
                Track(
                    id=identifier,
                    title="Corrected title",
                    artist="Corrected artist",
                    generation=1,
                    duration_seconds=10,
                    s3_prefix=f"tracks/{identifier}/1",
                    manifest_key=f"tracks/{identifier}/1/manifest.json",
                )
            )
        async with AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client:
            result = (await client.get(f"/api/tracks/{identifier}/manifest")).json()
            assert result["title"] == "Corrected title" and result["artist"] == "Corrected artist"
            assert result["integrity"] == manifest["integrity"]


async def test_incomplete_finalize_can_resume(settings, storage, manifest):
    settings.shizzle_completed_imports_enabled = True
    settings.shizzle_passcode = "fixture-only"
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="https://test") as client,
    ):
        token, _ = create_device_token(settings)
        client.headers["Authorization"] = "Bearer " + token
        created = (await client.post("/api/imports", json=manifest)).json()
        path = f"/api/imports/{created['importId']}"
        assert (await client.post(path + "/finalize")).status_code == 409
        assert (await client.get(path)).json()["status"] == "uploading"
        repo = imports.repository(type("R", (), {"app": app})())
        import uuid

        item = await repo.get(uuid.UUID(created["importId"]))
        put_inputs(storage, settings, item)
        assert (await client.post(path + "/finalize")).json()["status"] == "validating"
        assert (await client.post(path + "/uploads")).json()["uploads"] == []

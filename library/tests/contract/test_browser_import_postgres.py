"""Contract tests for the drop-box ingest against real Postgres (issue #45).

(a) a drop can never resurrect a soft-deleted track (C5/C8); (b) a drop
against a row whose generation advanced past 1 with a different published
manifest is rejected and the row is unchanged (the issue #33 class). S3 is
moto; the database is the compose Postgres via the shared contract fixtures.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from shizzle_server.db.repository import track_id_for_import
from shizzle_server.publish import browser_import
from shizzle_server.publish.browser_import import ImportRejected
from shizzle_server.publish.delivery_profile import CANONICAL_STEM_IDS

from .conftest import PG_URL

BUCKET = "shizzle-import-contract"
ROLES = CANONICAL_STEM_IDS
MEDIA_FILES = [f"stems/{role}.m4a" for role in ROLES] + ["video.mp4"]

pytestmark = pytest.mark.postgres


@pytest.fixture
def fake_aws_credentials(monkeypatch):
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


@pytest.fixture
def s3(fake_aws_credentials):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def passing_audits(monkeypatch, tmp_path: Path):
    def audit(path: Path, artifact: str, duration: float) -> dict[str, Any]:
        return {
            "artifact": artifact,
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "passed": True,
            "full_decode": "pass",
            "issues": [],
            "probe": {"format": {"duration": str(duration)}, "streams": []},
        }

    monkeypatch.setattr(
        browser_import,
        "audit_audio_file",
        lambda path, *, artifact, expected_duration, **_kw: audit(path, artifact, expected_duration),
    )
    monkeypatch.setattr(
        browser_import,
        "audit_video_file",
        lambda path, *, artifact, expected_duration, **_kw: audit(path, artifact, expected_duration),
    )


def _drop_media() -> dict[str, bytes]:
    return {file: f"contract-bytes:{file}".encode() * 3 for file in MEDIA_FILES}


def _manifest(media: dict[str, bytes]) -> dict[str, Any]:
    return {
        "version": 3,
        "delivery_profile": "shizzle-browser-v1",
        "title": "Contract Drop",
        "artist": "Contractor",
        "duration": 10.0,
        "video": "video.mp4",
        "stems": [
            {"id": role, "name": role.title(), "file": f"stems/{role}.m4a", "default_gain_db": 0.0}
            for role in ROLES
        ],
        "timeline": {"start_ms": 0, "duration_ms": 10_000, "sample_rate_hz": 44100},
        "integrity": {
            "source": "browser",
            "objects": [
                {
                    "file": file,
                    "bytes": len(media[file]),
                    "sha256": hashlib.sha256(media[file]).hexdigest(),
                }
                for file in sorted(media)
            ],
        },
    }


def _seed_drop(s3, ref: str) -> dict[str, Any]:
    media = _drop_media()
    manifest = _manifest(media)
    for file, body in media.items():
        s3.put_object(Bucket=BUCKET, Key=f"imports/{ref}/{file}", Body=body)
    s3.put_object(
        Bucket=BUCKET, Key=f"imports/{ref}/manifest.json", Body=json.dumps(manifest).encode()
    )
    return manifest


def _ingest(s3, ref: str) -> dict[str, Any]:
    return browser_import.ingest(
        s3=s3,
        bucket=BUCKET,
        source_ref=ref,
        database_url=PG_URL,
        max_duration_seconds=1800.0,
    )


def _run(coro):
    return asyncio.run(coro)


def test_ingest_cannot_resurrect_soft_deleted_track(
    pg_repos, migrated_database, s3, passing_audits  # noqa: ARG001
):
    _jobs, tracks, _heartbeats = pg_repos
    ref = "youtube-pgdeleted001"
    _seed_drop(s3, ref)
    tid = track_id_for_import(ref)

    assert _ingest(s3, ref)["status"] == "published"
    assert _run(tracks.soft_delete(tid))

    _seed_drop(s3, ref)  # identical re-drop after deletion
    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref)
    assert excinfo.value.code == "TRACK_DELETED"

    row = _run(tracks.get(tid))
    assert row is not None and row.deleted_at is not None  # still deleted
    assert _run(tracks.list_tracks()) == []  # and not listed


def test_advanced_generation_with_different_manifest_is_rejected_unchanged(
    pg_repos, migrated_database, s3, passing_audits  # noqa: ARG001
):
    from shizzle_server.publish.publisher import manifest_key

    _jobs, tracks, _heartbeats = pg_repos
    ref = "youtube-pgadvanced001"
    tid = track_id_for_import(ref)

    # The row advanced to generation 2 (a repair migration), whose published
    # manifest is different from anything the drop-box would produce.
    other_manifest = json.dumps({"version": 3, "title": "Repaired Mix"}).encode()
    for file in MEDIA_FILES:
        s3.put_object(Bucket=BUCKET, Key=f"tracks/{tid}/2/{file}", Body=b"repaired")
    s3.put_object(Bucket=BUCKET, Key=manifest_key(tid, 2), Body=other_manifest)
    _run(
        tracks.upsert_imported(
            tid,
            title="Repaired Mix",
            artist="",
            duration_seconds=10.0,
            s3_prefix=f"tracks/{tid}/2",
            manifest_key=manifest_key(tid, 2),
            generation=2,
            integrity={"source": "migration"},
        )
    )

    _seed_drop(s3, ref)
    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref)
    assert excinfo.value.code == "TRACK_CONFLICT"

    row = _run(tracks.get(tid))
    assert row is not None
    assert row.generation == 2  # unchanged
    assert row.title == "Repaired Mix"
    assert row.manifest_key == manifest_key(tid, 2)

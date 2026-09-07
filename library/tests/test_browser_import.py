"""Unit tests for the drop-box ingest (``publish/browser_import.py``, issue #45).

moto-backed S3 like ``test_lossless_intake.py``; the ffprobe/ffmpeg auditors
are monkeypatched with passing fakes (fake media bytes are fine — sha256 is
computed over whatever bytes we seed). DB paths run against the conftest
SQLite via ``database_url=settings.database_url``; the real-Postgres contract
variants live in ``tests/contract/test_browser_import_postgres.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from shizzle_server.db.repository import track_id_for_import
from shizzle_server.publish import browser_import
from shizzle_server.publish.browser_import import (
    MAX_IMPORT_VIDEO_BYTES,
    ImportNotReady,
    ImportRejected,
)
from shizzle_server.publish.delivery_profile import CANONICAL_STEM_IDS

BUCKET = "shizzle-import-test"
ROLES = CANONICAL_STEM_IDS
MEDIA_FILES = [f"stems/{role}.m4a" for role in ROLES] + ["video.mp4"]


# --- fixtures (copied from test_lossless_intake.py) ---------------------------


@pytest.fixture
def s3(fake_aws_credentials):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


#: Sample rate the passing audit fakes report; tests override it (R16).
_FAKE_RATE = {"hz": 44100}


def _audit(path: Path, artifact: str, duration: float) -> dict[str, Any]:
    return {
        "artifact": artifact,
        "bytes": path.stat().st_size,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "passed": True,
        "full_decode": "pass",
        "issues": [],
        "probe": {
            "format": {"duration": str(duration)},
            "streams": [
                {"codec_type": "audio", "sample_rate": str(_FAKE_RATE["hz"]),
                 "duration": str(duration)}
            ],
        },
    }


@pytest.fixture
def passing_audits(monkeypatch):
    def fake_audio(path, *, artifact, expected_duration, preserve_existing_lossy, **_kw):
        # R5: dropped bytes are preserved as-is — the flag must never be removed.
        assert preserve_existing_lossy is True, "browser imports preserve existing lossy audio"
        return _audit(path, artifact, expected_duration)

    monkeypatch.setattr(browser_import, "audit_audio_file", fake_audio)
    monkeypatch.setattr(
        browser_import,
        "audit_video_file",
        lambda path, *, artifact, expected_duration, **_kw: _audit(path, artifact, expected_duration),
    )


# --- drop construction helpers ------------------------------------------------


def _drop_media() -> dict[str, bytes]:
    return {file: f"browser-import-bytes:{file}".encode() * 3 for file in MEDIA_FILES}


def _manifest(
    media: dict[str, bytes],
    *,
    duration: float = 10.0,
    title: str = "Dropped Track",
    artist: str = "The Dropper",
) -> dict[str, Any]:
    return {
        "version": 3,
        "delivery_profile": "shizzle-browser-v1",
        "title": title,
        "artist": artist,
        "duration": duration,
        "video": "video.mp4",
        "stems": [
            {
                "id": role,
                "name": role.title(),
                "file": f"stems/{role}.m4a",
                "default_gain_db": 0.0,
            }
            for role in ROLES
        ],
        "timeline": {
            "start_ms": 0,
            "duration_ms": round(duration * 1000),
            "sample_rate_hz": 44100,
        },
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


def _seed_drop(
    s3,
    ref: str,
    *,
    media: dict[str, bytes] | None = None,
    manifest: dict[str, Any] | None = None,
    skip: tuple[str, ...] = (),
    extra: dict[str, bytes] | None = None,
    upload_manifest: bool = True,
) -> dict[str, Any]:
    media = media if media is not None else _drop_media()
    manifest = manifest if manifest is not None else _manifest(media)
    prefix = f"imports/{ref}/"
    for file in MEDIA_FILES:
        if file not in skip:
            s3.put_object(Bucket=BUCKET, Key=f"{prefix}{file}", Body=media[file])
    for key, body in (extra or {}).items():
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}{key}", Body=body)
    if upload_manifest:
        s3.put_object(
            Bucket=BUCKET,
            Key=f"{prefix}manifest.json",
            Body=json.dumps(manifest).encode(),
        )
    return manifest


def _ingest(s3, ref: str, *, database_url: str | None = None, **kwargs: Any) -> dict[str, Any]:
    return browser_import.ingest(
        s3=s3,
        bucket=BUCKET,
        source_ref=ref,
        database_url=database_url,
        max_duration_seconds=1800.0,
        **kwargs,
    )


def _read_result(s3, ref: str) -> dict[str, Any]:
    body = s3.get_object(Bucket=BUCKET, Key=f"imports/{ref}/result.json")["Body"].read()
    return json.loads(body)


def _keys(s3, prefix: str) -> list[str]:
    from shizzle_server.publish.publisher import Publisher

    return sorted(Publisher(s3, BUCKET).list_prefix(prefix))


def _run(coro):
    return asyncio.run(coro)


class RecordingS3:
    """Thin proxy that records the ordered API calls the ingest makes."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr) or name == "get_paginator":
            return attr

        def wrapped(*args, **kwargs):
            self.calls.append((name, kwargs))
            return attr(*args, **kwargs)

        return wrapped

    def get_paginator(self, name):
        return self._inner.get_paginator(name)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]


# --- source_ref namespace -----------------------------------------------------


def test_source_ref_outside_namespace_refused_before_any_s3_call():
    class ExplodingS3:
        def __getattr__(self, name):
            raise AssertionError(f"S3 was called: {name}")

    for bad in ("karaoke/pub/x", "youtube-ab", "imports/whatever", "soundcloud-xyz123", ""):
        with pytest.raises(ValueError, match="source_ref"):
            browser_import.ingest(
                s3=ExplodingS3(),
                bucket=BUCKET,
                source_ref=bad,
                database_url=None,
                max_duration_seconds=1800.0,
            )


def test_not_ready_raises_and_writes_no_result(s3):
    ref = "youtube-notready01"
    _seed_drop(s3, ref, upload_manifest=False)
    with pytest.raises(ImportNotReady):
        _ingest(s3, ref)
    with pytest.raises(ClientError):
        s3.head_object(Bucket=BUCKET, Key=f"imports/{ref}/result.json")


# --- manifest shape matrix ----------------------------------------------------


def _mutate_stem_too_large(manifest: dict[str, Any]) -> None:
    from shizzle_server.publish.publisher import MAX_STEM_BYTES

    for entry in manifest["integrity"]["objects"]:
        if entry["file"] == "stems/vocals.m4a":
            entry["bytes"] = MAX_STEM_BYTES + 1


def _mutate_video_too_large(manifest: dict[str, Any]) -> None:
    for entry in manifest["integrity"]["objects"]:
        if entry["file"] == "video.mp4":
            entry["bytes"] = MAX_IMPORT_VIDEO_BYTES + 1


def _mutations():
    return [
        pytest.param(lambda m: m.update(version=2), "manifest-version", id="version"),
        pytest.param(
            lambda m: m.update(delivery_profile="other-profile"), "manifest-profile", id="profile"
        ),
        pytest.param(lambda m: m["stems"].reverse(), "manifest-stem-order", id="stem-order"),
        pytest.param(
            lambda m: m["stems"][0].update(default_gain_db=1.5),
            "manifest-gain-positive",
            id="gain-positive",
        ),
        pytest.param(
            lambda m: m["stems"][3].update(default_gain_db=-3.0),
            "manifest-gain-not-common",
            id="gain-not-common",
        ),
        pytest.param(
            lambda m: m["stems"][1].update(default_gain_db="loud"),
            "manifest-gain-invalid",
            id="gain-not-numeric",
        ),
        pytest.param(lambda m: m.update(duration=0), "manifest-duration", id="duration-zero"),
        pytest.param(lambda m: m.update(duration=99999), "manifest-duration", id="duration-max"),
        pytest.param(
            lambda m: m["timeline"].update(start_ms=250), "manifest-timeline", id="timeline-start"
        ),
        pytest.param(
            lambda m: m["timeline"].update(sample_rate_hz=22050),
            "manifest-timeline",
            id="timeline-rate",
        ),
        pytest.param(
            lambda m: m["timeline"].update(sample_rate_hz=44100.0),
            "manifest-timeline",
            id="timeline-rate-float",
        ),
        pytest.param(
            lambda m: m["timeline"].update(start_ms=False),
            "manifest-timeline",
            id="timeline-start-false",
        ),
        pytest.param(
            lambda m: m["timeline"].update(duration_ms=float("nan")),
            "manifest-timeline",
            id="timeline-ms-nan",
        ),
        pytest.param(
            lambda m: m.update(artist=5), "manifest-artist", id="artist-type"
        ),
        pytest.param(
            lambda m: m["timeline"].update(duration_ms=m["timeline"]["duration_ms"] + 40),
            "manifest-timeline",
            id="timeline-ms",
        ),
        pytest.param(lambda m: m.update(title="   "), "manifest-title", id="title-empty"),
        pytest.param(
            lambda m: m["integrity"]["objects"].pop(0), "manifest-objects-missing", id="object-missing"
        ),
        pytest.param(
            lambda m: m["integrity"]["objects"].append(
                {"file": "extra.bin", "bytes": 5, "sha256": "0" * 64}
            ),
            "manifest-objects-extra",
            id="object-extra",
        ),
        pytest.param(
            lambda m: m["integrity"]["objects"][0].update(bytes=0),
            "manifest-object-bytes",
            id="object-bytes",
        ),
        pytest.param(
            lambda m: m["integrity"]["objects"][0].update(sha256="NOTHEX"),
            "manifest-object-sha",
            id="object-sha",
        ),
        pytest.param(
            lambda m: m["timeline"].update(start_ms_offset=250),
            "manifest-timeline",
            id="timeline-extra-key",
        ),
        pytest.param(
            lambda m: m["timeline"].pop("start_ms"), "manifest-timeline", id="timeline-missing-key"
        ),
        pytest.param(
            lambda m: m["integrity"]["objects"].append(dict(m["integrity"]["objects"][0])),
            "manifest-object-duplicate",
            id="object-duplicate",
        ),
        pytest.param(_mutate_stem_too_large, "manifest-object-too-large", id="stem-too-large"),
        pytest.param(_mutate_video_too_large, "manifest-object-too-large", id="video-too-large"),
    ]


@pytest.mark.parametrize(("mutate", "expected_code"), _mutations())
def test_manifest_shape_matrix_rejects_with_stable_code(s3, mutate, expected_code):
    ref = f"youtube-matrix{expected_code}"[:128]
    media = _drop_media()
    manifest = _manifest(media)
    mutate(manifest)
    _seed_drop(s3, ref, media=media, manifest=manifest)

    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref)
    assert excinfo.value.code == "MANIFEST_INVALID"
    result = _read_result(s3, ref)
    assert result["status"] == "rejected"
    assert result["code"] == "MANIFEST_INVALID"
    assert expected_code in [issue["code"] for issue in result["issues"]]
    assert _keys(s3, "tracks/") == []  # rejected before any copy


def test_manifest_not_json_rejected(s3):
    ref = "youtube-badjson123"
    _seed_drop(s3, ref, upload_manifest=False)
    s3.put_object(Bucket=BUCKET, Key=f"imports/{ref}/manifest.json", Body=b"not json{")
    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref)
    assert excinfo.value.code == "MANIFEST_INVALID"
    assert _read_result(s3, ref)["code"] == "MANIFEST_INVALID"


# --- inventory gates ----------------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "skip", "label"),
    [
        ({"notes.txt": b"hello"}, (), "extra-object"),
        ({}, ("stems/bass.m4a",), "missing-object"),
    ],
)
def test_inventory_mismatch_rejected_drop_intact_nothing_copied(s3, extra, skip, label):
    ref = f"youtube-inv-{label}"
    _seed_drop(s3, ref, skip=skip, extra=extra)
    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref)
    assert excinfo.value.code == "INVENTORY_MISMATCH"
    assert _read_result(s3, ref)["code"] == "INVENTORY_MISMATCH"
    assert _keys(s3, "tracks/") == []
    # drop left intact: exactly what was seeded, plus the ingest result
    expected = (
        (set(MEDIA_FILES) - set(skip)) | {"manifest.json"} | set(extra) | {"result.json"}
    )
    assert set(_keys(s3, f"imports/{ref}/")) == expected


def test_inventory_size_mismatch_rejected(s3):
    ref = "youtube-sizemismatch"
    media = _drop_media()
    manifest = _manifest(media)
    manifest["integrity"]["objects"][0]["bytes"] += 5  # declared != listed size
    _seed_drop(s3, ref, media=media, manifest=manifest)
    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref)
    assert excinfo.value.code == "INVENTORY_MISMATCH"


# --- integrity gates (C6/C8) --------------------------------------------------


def test_sha_mismatch_rejected_no_generation_no_row(s3, settings, engine, track_repo, passing_audits, monkeypatch):  # noqa: ARG001
    ref = "youtube-shamismatch1"
    media = _drop_media()
    manifest = _manifest(media)
    for entry in manifest["integrity"]["objects"]:
        if entry["file"] == "stems/vocals.m4a":
            entry["sha256"] = "0" * 64  # right size, wrong digest
    _seed_drop(s3, ref, media=media, manifest=manifest)
    tid = track_id_for_import(ref)

    audited: list[str] = []
    real_audio = browser_import.audit_audio_file

    def recording_audio(path, **kwargs):
        audited.append(kwargs["artifact"])
        return real_audio(path, **kwargs)

    monkeypatch.setattr(browser_import, "audit_audio_file", recording_audio)

    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref, database_url=settings.database_url)
    assert excinfo.value.code == "INTEGRITY_GATE_FAILED"
    assert any(i["code"] == "sha256-mismatch" for i in _read_result(s3, ref)["issues"])
    # R4: known-bad bytes are never fully decoded — vocals is not audited.
    assert "stems/vocals.m4a" not in audited
    assert "stems/drums.m4a" in audited
    with pytest.raises(ClientError):
        s3.head_object(Bucket=BUCKET, Key=f"tracks/{tid}/1/manifest.json")
    assert _run(track_repo.get(tid)) is None  # C6/C8: no row before gates pass


def test_audit_error_rejected_no_generation_no_row(s3, settings, engine, track_repo, monkeypatch):
    def failing_audio(path, *, artifact, expected_duration, **_kw):
        return {
            "artifact": artifact,
            "bytes": path.stat().st_size,
            "sha256": "a" * 64,
            "passed": False,
            "full_decode": "fail",
            "issues": [{"code": "audio-full-decode", "message": "boom", "severity": "error"}],
            "probe": {"format": {"duration": str(expected_duration)}, "streams": []},
        }

    monkeypatch.setattr(browser_import, "audit_audio_file", failing_audio)
    monkeypatch.setattr(
        browser_import,
        "audit_video_file",
        lambda path, *, artifact, expected_duration, **_kw: _audit(path, artifact, expected_duration),
    )
    ref = "youtube-auditfail001"
    _seed_drop(s3, ref)
    tid = track_id_for_import(ref)

    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref, database_url=settings.database_url)
    assert excinfo.value.code == "INTEGRITY_GATE_FAILED"
    with pytest.raises(ClientError):
        s3.head_object(Bucket=BUCKET, Key=f"tracks/{tid}/1/manifest.json")
    assert _run(track_repo.get(tid)) is None


def test_inter_stem_spread_over_tolerance_rejected(s3, passing_audits, monkeypatch):
    durations = dict.fromkeys(ROLES, 10.0)
    durations["piano"] = 10.020  # 20 ms spread > the 5 ms bound (D2)

    def spread_audio(path, *, artifact, expected_duration, **_kw):
        audit = _audit(path, artifact, expected_duration)
        role = artifact.split("/")[1].split(".")[0]
        audit["probe"]["format"]["duration"] = str(durations[role])
        return audit

    monkeypatch.setattr(browser_import, "audit_audio_file", spread_audio)
    ref = "youtube-spread000001"
    _seed_drop(s3, ref)

    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref)
    assert excinfo.value.code == "INTEGRITY_GATE_FAILED"
    assert any(i["code"] == "stem-inter-duration-spread" for i in _read_result(s3, ref)["issues"])


def test_total_bitrate_over_budget_rejected(s3, passing_audits):
    ref = "youtube-bitrate0001"
    media = _drop_media()  # ~small bytes but a near-zero duration blows the budget
    manifest = _manifest(media, duration=0.001)
    _seed_drop(s3, ref, media=media, manifest=manifest)

    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref)
    assert excinfo.value.code == "INTEGRITY_GATE_FAILED"
    assert any(i["code"] == "total-average-bitrate" for i in _read_result(s3, ref)["issues"])


# --- happy path and convergence ----------------------------------------------


def test_happy_path_publishes_and_registers(
    s3, settings, engine, track_repo, passing_audits, tmp_path
):
    ref = "youtube-happy0000001"
    manifest = _seed_drop(s3, ref)
    tid = track_id_for_import(ref)

    result = _ingest(s3, ref, database_url=settings.database_url, workdir=tmp_path / "dl")

    assert result["status"] == "published"
    assert result["trackId"] == str(tid)
    assert result["generation"] == 1
    dropped_manifest_sha = browser_import.manifest_sha256(
        json.dumps(manifest).encode()
    )
    assert result["manifestSha256"] == dropped_manifest_sha  # R6 correlation
    published = s3.get_object(Bucket=BUCKET, Key=f"tracks/{tid}/1/manifest.json")
    assert json.loads(published["Body"].read())["title"] == "Dropped Track"

    row = _run(track_repo.get(tid))
    assert row is not None
    assert row.s3_prefix == f"tracks/{tid}/1"
    assert row.manifest_key == f"tracks/{tid}/1/manifest.json"
    assert row.generation == 1
    assert row.title == "Dropped Track"
    assert row.artist == "The Dropper"
    assert [str(t.id) for t in _run(track_repo.list_tracks())] == [str(tid)]
    assert row.integrity["source"] == "browser-import"
    assert row.integrity["publisher"]["policy"]  # staged verification recorded

    from shizzle_server.publish.publisher import staging_prefix

    assert _keys(s3, staging_prefix(tid, 1)) == []  # staging cleaned
    # dropped media deleted; manifest.json + result.json remain
    assert _keys(s3, f"imports/{ref}/") == ["manifest.json", "result.json"]
    assert _read_result(s3, ref)["status"] == "published"


def test_rerun_after_success_cleanup_is_already_published(
    s3, settings, engine, track_repo, passing_audits, tmp_path
):
    """A plain retry of the SAME command after success must converge.

    Success cleanup deletes the seven dropped media objects, so the identity
    check has to run before inventory (live-run finding, corrective round 1).
    """
    ref = "youtube-rerunok000001"
    _seed_drop(s3, ref)
    tid = track_id_for_import(ref)
    assert (
        _ingest(s3, ref, database_url=settings.database_url, workdir=tmp_path / "a")["status"]
        == "published"
    )
    assert _keys(s3, f"imports/{ref}/") == ["manifest.json", "result.json"]  # media gone
    before = _run(track_repo.get(tid))

    rec = RecordingS3(s3)
    result = _ingest(rec, ref, database_url=settings.database_url)

    assert result["status"] == "already-published"
    assert "copy_object" not in rec.names()
    assert "download_file" not in rec.names()
    assert _read_result(s3, ref)["status"] == "already-published"
    after = _run(track_repo.get(tid))
    for field in (
        "title", "artist", "generation", "s3_prefix", "manifest_key",
        "integrity", "created_at", "deleted_at",
    ):
        assert getattr(after, field) == getattr(before, field)  # row untouched


def test_missing_object_drop_against_published_generation_is_already_published(
    s3, passing_audits
):
    """Identity precedes inventory: a partial re-drop of identical content
    against its own published generation is a converged retry, not a mismatch."""
    ref = "youtube-partialredrop1"
    manifest = _manifest(_drop_media())
    _seed_drop(s3, ref, manifest=manifest)
    assert _ingest(s3, ref)["status"] == "published"

    # Producer re-drops only the manifest (media upload interrupted).
    s3.put_object(
        Bucket=BUCKET,
        Key=f"imports/{ref}/manifest.json",
        Body=json.dumps(manifest).encode(),
    )
    result = _ingest(s3, ref)
    assert result["status"] == "already-published"
    assert _read_result(s3, ref)["status"] == "already-published"


def test_publish_race_with_different_content_is_conflict(
    s3, settings, engine, track_repo, passing_audits, tmp_path, monkeypatch
):
    """Two ingests of different content under one ref: the loser must never
    register its manifest over the winner's generation or row."""
    from shizzle_server.publish.publisher import Publisher, PublishResult

    ref = "youtube-racepublish1"
    _seed_drop(s3, ref)
    tid = track_id_for_import(ref)

    def racing_publish(self, track_id_, generation_, staged_):
        # The rival's manifest lands under the generation key first; our
        # publish() then no-ops (C1) and returns already_published.
        s3.put_object(
            Bucket=BUCKET,
            Key=f"tracks/{track_id_}/{generation_}/manifest.json",
            Body=json.dumps({"version": 3, "title": "Rival"}).encode(),
        )
        return PublishResult(
            track_id=str(track_id_),
            generation=generation_,
            s3_prefix=f"tracks/{track_id_}/{generation_}",
            manifest_key=f"tracks/{track_id_}/{generation_}/manifest.json",
            already_published=True,
        )

    monkeypatch.setattr(Publisher, "publish", racing_publish)

    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref, database_url=settings.database_url, workdir=tmp_path / "dl")

    assert excinfo.value.code == "TRACK_CONFLICT"
    assert _run(track_repo.get(tid)) is None  # nothing registered
    assert len(_keys(s3, f"imports/{ref}/")) == len(MEDIA_FILES) + 2  # no cleanup
    assert _read_result(s3, ref)["code"] == "TRACK_CONFLICT"


def test_register_race_with_different_row_hash_is_conflict(
    s3, settings, engine, track_repo, monkeypatch
):
    """A row appearing mid-flight with a different recorded manifest hash is
    a conflict; the row stays byte-identical and ours is never written."""
    ref = "youtube-raceregister1"
    manifest = _seed_drop(s3, ref)
    tid = track_id_for_import(ref)

    # Our manifest AND media are already published (a completed retry as far
    # as the identity check and landed-generation verification are concerned).
    for file, body in {**_drop_media(), "manifest.json": json.dumps(manifest).encode()}.items():
        s3.put_object(Bucket=BUCKET, Key=f"tracks/{tid}/1/{file}", Body=body)
    # A rival ingest registers the row between our identity check and our
    # registration, recording a different manifest hash.
    _run(
        track_repo.upsert_imported(
            tid,
            title="Rival Row",
            artist="",
            duration_seconds=10.0,
            s3_prefix=f"tracks/{tid}/1",
            manifest_key=f"tracks/{tid}/1/manifest.json",
            generation=1,
            integrity={"source": "browser-import", "manifest_sha256": "f" * 64},
        )
    )
    before = _run(track_repo.get(tid))
    assert before is not None

    reads = {"n": 0}
    real_get = browser_import._get_track

    def racing_get(database_url, track_id_):
        reads["n"] += 1
        if reads["n"] == 1:
            return None  # the identity check still saw no row
        return real_get(database_url, track_id_)  # the pre-register re-read sees the rival

    monkeypatch.setattr(browser_import, "_get_track", racing_get)

    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref, database_url=settings.database_url)

    assert excinfo.value.code == "TRACK_CONFLICT"
    after = _run(track_repo.get(tid))
    for field in ("title", "artist", "generation", "s3_prefix", "manifest_key", "integrity"):
        assert getattr(after, field) == getattr(before, field)  # row byte-identical
    assert _read_result(s3, ref)["code"] == "TRACK_CONFLICT"


def test_rerun_after_crash_between_publish_and_register(
    s3, settings, engine, track_repo, passing_audits
):
    ref = "sha256-crashrerun001"
    manifest = _seed_drop(s3, ref)
    tid = track_id_for_import(ref)
    # Crash simulation: the generation was fully published, the row never came.
    s3.copy_object(
        Bucket=BUCKET,
        Key=f"tracks/{tid}/1/manifest.json",
        CopySource={"Bucket": BUCKET, "Key": f"imports/{ref}/manifest.json"},
    )
    for file in MEDIA_FILES:
        s3.copy_object(
            Bucket=BUCKET,
            Key=f"tracks/{tid}/1/{file}",
            CopySource={"Bucket": BUCKET, "Key": f"imports/{ref}/{file}"},
        )

    rec = RecordingS3(s3)
    result = _ingest(rec, ref, database_url=settings.database_url)

    assert result["status"] == "published"
    assert "copy_object" not in rec.names()  # no re-copy
    assert "download_file" not in rec.names()  # no re-download either
    row = _run(track_repo.get(tid))
    assert row is not None
    assert row.manifest_key == f"tracks/{tid}/1/manifest.json"
    assert json.loads(
        s3.get_object(Bucket=BUCKET, Key=f"tracks/{tid}/1/manifest.json")["Body"].read()
    ) == manifest
    assert _keys(s3, f"imports/{ref}/") == ["manifest.json", "result.json"]


def test_same_content_rerun_is_already_published_row_untouched(
    s3, settings, engine, track_repo, passing_audits
):
    ref = "youtube-samecontent1"
    manifest = _manifest(_drop_media())
    _seed_drop(s3, ref, manifest=manifest)
    tid = track_id_for_import(ref)
    assert _ingest(s3, ref, database_url=settings.database_url)["status"] == "published"

    # Operator edits the row; a re-drop of the identical package must not
    # touch it.
    _run(
        track_repo.upsert_imported(
            tid,
            title="EDITED",
            artist="",
            duration_seconds=10.0,
            s3_prefix=f"tracks/{tid}/1",
            manifest_key=f"tracks/{tid}/1/manifest.json",
            generation=1,
            integrity={"source": "manual"},
        )
    )
    _seed_drop(s3, ref, manifest=manifest)  # producer re-drops the same bytes

    result = _ingest(s3, ref, database_url=settings.database_url)
    assert result["status"] == "already-published"
    assert _run(track_repo.get(tid)).title == "EDITED"
    assert _read_result(s3, ref)["status"] == "already-published"


def test_different_content_conflict_refused_row_untouched(
    s3, settings, engine, track_repo, passing_audits
):
    ref = "youtube-conflicting001"
    _seed_drop(s3, ref, manifest=_manifest(_drop_media()))
    tid = track_id_for_import(ref)
    assert _ingest(s3, ref, database_url=settings.database_url)["status"] == "published"
    generation_keys = _keys(s3, f"tracks/{tid}/1/")

    # Different content under the same ref: same media, different manifest.
    media = _drop_media()
    _seed_drop(s3, ref, media=media, manifest=_manifest(media, title="Different Mix"))

    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref, database_url=settings.database_url)
    assert excinfo.value.code == "TRACK_CONFLICT"
    assert _read_result(s3, ref)["code"] == "TRACK_CONFLICT"
    assert _run(track_repo.get(tid)).title == "Dropped Track"  # row untouched
    assert _keys(s3, f"tracks/{tid}/1/") == generation_keys  # nothing new written


def test_soft_deleted_row_rejected_never_resurrected(
    s3, settings, engine, track_repo, passing_audits
):
    ref = "youtube-deleted000001"
    manifest = _manifest(_drop_media())
    _seed_drop(s3, ref, manifest=manifest)
    tid = track_id_for_import(ref)
    assert _ingest(s3, ref, database_url=settings.database_url)["status"] == "published"
    assert _run(track_repo.soft_delete(tid))

    _seed_drop(s3, ref, manifest=manifest)  # identical re-drop
    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref, database_url=settings.database_url)
    assert excinfo.value.code == "TRACK_DELETED"
    row = _run(track_repo.get(tid))
    assert row is not None and row.deleted_at is not None  # C5: stays deleted
    assert _run(track_repo.list_tracks()) == []  # not listed


def test_dry_run_validates_but_touches_nothing(s3, settings, engine, track_repo, passing_audits, tmp_path):  # noqa: ARG001
    ref = "youtube-dryrun000001"
    _seed_drop(s3, ref)
    tid = track_id_for_import(ref)

    result = _ingest(
        s3, ref, database_url=settings.database_url, dry_run=True, workdir=tmp_path / "dl"
    )

    assert result["status"] == "would-publish"
    assert _read_result(s3, ref)["status"] == "would-publish"
    assert _keys(s3, "tracks/") == []
    assert _run(track_repo.get(tid)) is None
    assert len(_keys(s3, f"imports/{ref}/")) == len(MEDIA_FILES) + 2  # drop fully intact


def test_no_database_url_publishes_without_row(s3, passing_audits, tmp_path):
    ref = "youtube-nodb00000001"
    _seed_drop(s3, ref)
    tid = track_id_for_import(ref)

    result = _ingest(s3, ref, workdir=tmp_path / "dl")
    assert result["status"] == "published"
    assert _keys(s3, f"tracks/{tid}/1/") == sorted([*MEDIA_FILES, "manifest.json"])
    assert _read_result(s3, ref)["status"] == "published"


def test_no_database_url_rerun_same_content_already_published(s3, passing_audits, tmp_path):
    ref = "youtube-nodbsame0001"
    manifest = _manifest(_drop_media())
    _seed_drop(s3, ref, manifest=manifest)
    assert _ingest(s3, ref, workdir=tmp_path)["status"] == "published"
    _seed_drop(s3, ref, manifest=manifest)
    assert _ingest(s3, ref, workdir=tmp_path)["status"] == "already-published"


# --- CLI exit codes -----------------------------------------------------------


def test_cli_exit_codes(s3, settings, engine, passing_audits, tmp_path, monkeypatch):
    ok_ref = "youtube-cliok0000001"
    _seed_drop(s3, ok_ref)

    def run_cli(ref: str) -> int:
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "browser_import",
                "--source-ref",
                ref,
                "--bucket",
                BUCKET,
                "--database-url",
                settings.database_url,
                "--workdir",
                str(tmp_path / "dl"),
            ],
        )
        with pytest.raises(SystemExit) as excinfo:
            browser_import.main()
        return int(excinfo.value.code)

    assert run_cli(ok_ref) == 0
    # Retry of the same command after success (media deleted by cleanup):
    # already-published, still exit 0.
    assert run_cli(ok_ref) == 0

    rejected_ref = "youtube-clibad0000001"
    _seed_drop(s3, rejected_ref, skip=("stems/drums.m4a",))
    assert run_cli(rejected_ref) == 2

    not_ready_ref = "youtube-clinr00000001"
    _seed_drop(s3, not_ready_ref, upload_manifest=False)
    assert run_cli(not_ready_ref) == 3


# --- landed-generation verification (R1/R14) ----------------------------------


def _seed_generation(s3, tid: uuid.UUID, manifest: dict[str, Any]) -> None:
    """Publish a complete generation by hand (crash-between-publish-and-register)."""
    payload = {**_drop_media(), "manifest.json": json.dumps(manifest).encode()}
    for file, body in payload.items():
        s3.put_object(Bucket=BUCKET, Key=f"tracks/{tid}/1/{file}", Body=body)


def test_completed_retry_with_altered_published_stem_rejected(
    s3, settings, engine, track_repo, passing_audits
):
    ref = "sha256-retryaltered1"
    manifest = _seed_drop(s3, ref)
    tid = track_id_for_import(ref)
    _seed_generation(s3, tid, manifest)
    # The published stem was corrupted after the crash.
    s3.put_object(Bucket=BUCKET, Key=f"tracks/{tid}/1/stems/vocals.m4a", Body=b"tampered-stem")

    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref, database_url=settings.database_url)

    assert excinfo.value.code == "GENERATION_UNVERIFIED"
    assert _run(track_repo.get(tid)) is None  # no row
    assert len(_keys(s3, f"imports/{ref}/")) == len(MEDIA_FILES) + 2  # drop left intact
    assert _read_result(s3, ref)["code"] == "GENERATION_UNVERIFIED"


def test_generation_tampered_after_publish_is_unverified(
    s3, settings, engine, track_repo, passing_audits, tmp_path, monkeypatch
):
    """R14: even a fresh publish is re-verified before registering — a rival
    that mixed its media into our generation makes it unregisterable."""
    from shizzle_server.publish.publisher import Publisher

    ref = "youtube-tamperpublish"
    _seed_drop(s3, ref)
    tid = track_id_for_import(ref)
    real_publish = Publisher.publish

    def tampering_publish(self, track_id_, generation_, staged_):
        result = real_publish(self, track_id_, generation_, staged_)
        s3.put_object(
            Bucket=BUCKET,
            Key=f"tracks/{track_id_}/{generation_}/stems/vocals.m4a",
            Body=b"tampered",
        )
        return result

    monkeypatch.setattr(Publisher, "publish", tampering_publish)

    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref, database_url=settings.database_url, workdir=tmp_path / "dl")

    assert excinfo.value.code == "GENERATION_UNVERIFIED"
    assert _run(track_repo.get(tid)) is None


# --- timeline sample-rate agreement (R16) -------------------------------------


def test_timeline_48000_accepted_when_stems_agree(s3, passing_audits, monkeypatch, tmp_path):
    monkeypatch.setitem(_FAKE_RATE, "hz", 48000)
    ref = "youtube-rate48000001"
    media = _drop_media()
    manifest = _manifest(media)
    manifest["timeline"]["sample_rate_hz"] = 48000
    _seed_drop(s3, ref, media=media, manifest=manifest)

    assert _ingest(s3, ref, workdir=tmp_path / "dl")["status"] == "published"


def test_stem_rate_mismatch_with_timeline_rejected(s3, passing_audits, monkeypatch):
    monkeypatch.setitem(_FAKE_RATE, "hz", 48000)  # timeline still declares 44100
    ref = "youtube-ratemismatch1"
    _seed_drop(s3, ref)

    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref)
    assert excinfo.value.code == "INTEGRITY_GATE_FAILED"
    assert any(
        i["code"] == "stem-sample-rate-mismatch" for i in _read_result(s3, ref)["issues"]
    )


# --- publisher-failure cleanup and idempotent drop cleanup (R18/R19) ----------


def test_publish_failure_clears_incomplete_prefixes(
    s3, settings, engine, track_repo, passing_audits, tmp_path, monkeypatch
):
    from shizzle_server.publish.publisher import PromotionFailed, Publisher, generation_prefix

    ref = "youtube-promotefail01"
    _seed_drop(s3, ref)
    tid = track_id_for_import(ref)

    def failing_promote(self, track_id_, generation_, report_):
        media = sorted(
            (o for o in report_.objects if o.file != "manifest.json"), key=lambda o: o.file
        )
        for outcome in media[:2]:  # two copies land, then the promotion dies
            self.copy_object(
                outcome.staging_key,
                f"{generation_prefix(track_id_, generation_)}{outcome.file}",
                outcome.actual_size or 0,
            )
        raise PromotionFailed("simulated failure after two copies")

    monkeypatch.setattr(Publisher, "promote", failing_promote)

    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref, database_url=settings.database_url, workdir=tmp_path / "dl")

    assert excinfo.value.code == "INTEGRITY_GATE_FAILED"
    assert _keys(s3, f"tracks/{tid}/") == []  # generation AND staging cleared
    assert len(_keys(s3, f"imports/{ref}/")) == len(MEDIA_FILES) + 2  # drop intact
    assert _run(track_repo.get(tid)) is None


def test_already_published_full_redrop_cleans_dropped_media(
    s3, settings, engine, track_repo, passing_audits
):
    ref = "youtube-redropclean01"
    manifest = _manifest(_drop_media())
    _seed_drop(s3, ref, manifest=manifest)
    assert _ingest(s3, ref, database_url=settings.database_url)["status"] == "published"

    _seed_drop(s3, ref, manifest=manifest)  # producer re-drops identical content
    result = _ingest(s3, ref, database_url=settings.database_url)

    assert result["status"] == "already-published"
    assert _keys(s3, f"imports/{ref}/") == ["manifest.json", "result.json"]  # media removed


# --- strict pre-register row check (R20) --------------------------------------


def test_register_race_with_blank_row_hash_is_conflict(
    s3, settings, engine, track_repo, monkeypatch
):
    """A live row with no valid manifest_sha256 (e.g. written by another tool)
    is never overwritten by a drop."""
    ref = "youtube-raceblankrow1"
    manifest = _seed_drop(s3, ref)
    tid = track_id_for_import(ref)
    _seed_generation(s3, tid, manifest)
    _run(
        track_repo.upsert_imported(
            tid,
            title="Foreign Row",
            artist="",
            duration_seconds=10.0,
            s3_prefix=f"tracks/{tid}/1",
            manifest_key=f"tracks/{tid}/1/manifest.json",
            generation=1,
            integrity={},  # no manifest_sha256 recorded
        )
    )
    before = _run(track_repo.get(tid))

    reads = {"n": 0}
    real_get = browser_import._get_track

    def racing_get(database_url, track_id_):
        reads["n"] += 1
        return None if reads["n"] == 1 else real_get(database_url, track_id_)

    monkeypatch.setattr(browser_import, "_get_track", racing_get)

    with pytest.raises(ImportRejected) as excinfo:
        _ingest(s3, ref, database_url=settings.database_url)
    assert excinfo.value.code == "TRACK_CONFLICT"
    after = _run(track_repo.get(tid))
    for field in ("title", "artist", "generation", "s3_prefix", "manifest_key", "integrity"):
        assert getattr(after, field) == getattr(before, field)  # row byte-identical


# --- dry-run on the completed-retry path (R24) --------------------------------


def test_dry_run_completed_retry_is_would_register(
    s3, settings, engine, track_repo, passing_audits
):
    ref = "sha256-dryretry0001"
    manifest = _seed_drop(s3, ref)
    tid = track_id_for_import(ref)
    _seed_generation(s3, tid, manifest)

    result = _ingest(s3, ref, database_url=settings.database_url, dry_run=True)

    assert result["status"] == "would-register"
    assert _run(track_repo.get(tid)) is None  # no row
    assert len(_keys(s3, f"imports/{ref}/")) == len(MEDIA_FILES) + 2  # drop fully intact
    assert _read_result(s3, ref)["status"] == "would-register"


# --- sanitized error path (R2, E3) --------------------------------------------


def test_cli_error_never_prints_secrets(s3, settings, engine, monkeypatch, capsys):
    def exploding_ingest(**_kwargs):
        raise RuntimeError("boom http://user:SUPERSECRET@proxy.example end")

    monkeypatch.setattr(browser_import, "ingest", exploding_ingest)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "browser_import",
            "--source-ref",
            "youtube-secretleak1",
            "--bucket",
            BUCKET,
            "--database-url",
            settings.database_url,
        ],
    )
    with pytest.raises(SystemExit) as excinfo:
        browser_import.main()

    assert excinfo.value.code == 4
    captured = capsys.readouterr()
    assert "SUPERSECRET" not in captured.out + captured.err
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "error"
    assert payload["type"] == "RuntimeError"
    assert payload["message"] == "operation failed"

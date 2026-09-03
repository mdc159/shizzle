"""Publisher unit tests (moto-backed S3).

Covers the four properties Phase 3.4 depends on:
  1. verification catches missing / short / corrupted staged objects and the
     failure maps to ErrorCode.CHECKSUM_MISMATCH;
  2. promotion is a server-side copy (no download, no re-upload);
  3. the manifest is the LAST object written to the generation prefix;
  4. an already-published generation is never overwritten, and re-running a
     publish converges on the same tracks row.
"""

from __future__ import annotations

import hashlib
import json
import uuid

import boto3
import pytest
from moto import mock_aws

from shizzle_server.db.models import JobStage
from shizzle_server.db.repository import track_id_for_job
from shizzle_server.errors import ErrorCode
from shizzle_server.publish import (
    MANIFEST_NAME,
    MAX_STEM_BYTES,
    ChecksumMismatch,
    ChecksumPolicy,
    InvalidStemObject,
    ObjectVerification,
    Publisher,
    PublishError,
    StagedObject,
    StagedObjectMissing,
    _stored_sha256,
    generation_prefix,
    manifest_key,
    publish_job,
    staging_prefix,
    validate_stem_object,
    validate_stem_objects,
)

BUCKET = "shizzle-test-bucket"
TRACK_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
GENERATION = 1

MANIFEST = {
    "version": 3,
    "title": "Test Track",
    "artist": "Test Artist",
    "duration": 123.5,
    "video": "video.mp4",
    "stems": [
        {"id": "vocals", "name": "Vocals", "file": "stems/vocals.m4a", "default_gain_db": 0.0}
    ],
}

STAGED_FILES = {
    "video.mp4": b"video-bytes" * 100,
    "stems/vocals.m4a": b"vocals-bytes" * 100,
    "stems/drums.m4a": b"drums-bytes" * 100,
    "multi-track.mp4": b"multitrack-bytes" * 100,
    MANIFEST_NAME: json.dumps(MANIFEST).encode(),
}


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class RecordingS3:
    """Thin proxy that records the ordered API calls the publisher makes."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr) or name == "get_paginator":
            return attr

        def wrapped(**kwargs):
            self.calls.append((name, kwargs))
            return attr(**kwargs)

        return wrapped

    def get_paginator(self, name):
        self.calls.append(("get_paginator", {"name": name}))
        return self._inner.get_paginator(name)

    def names(self) -> list[str]:
        return [c[0] for c in self.calls]

    def copied_keys(self) -> list[str]:
        return [c[1]["Key"] for c in self.calls if c[0] == "copy_object"]


@pytest.fixture
def fake_aws_credentials(monkeypatch):
    """Never let this machine's real AWS credentials or the R2 endpoint override
    reach moto — the dev box has both set."""
    for var, value in {
        "AWS_ACCESS_KEY_ID": "testing" * 5,
        "AWS_SECRET_ACCESS_KEY": "testing" * 6,
        "AWS_SESSION_TOKEN": "testing",
        "AWS_SECURITY_TOKEN": "testing",
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


def _stage(s3, files: dict[str, bytes] | None = None, *, with_checksums: bool = False):
    """Upload the staging file set; returns the worker-style report."""
    files = STAGED_FILES if files is None else files
    prefix = staging_prefix(TRACK_ID, GENERATION)
    reported = []
    for rel, data in files.items():
        extra = {"ChecksumAlgorithm": "SHA256"} if with_checksums else {}
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}{rel}", Body=data, **extra)
        reported.append(StagedObject(file=rel, size_bytes=len(data), sha256=_sha(data)))
    return reported


# --- key layout ---------------------------------------------------------------


def test_key_layout_is_immutable_generation_shaped():
    assert generation_prefix(TRACK_ID, 2) == f"tracks/{TRACK_ID}/2/"
    assert staging_prefix(TRACK_ID, 2) == f"tracks/{TRACK_ID}/2/staging/"
    assert manifest_key(TRACK_ID, 2) == f"tracks/{TRACK_ID}/2/manifest.json"


def test_staged_object_parses_worker_result_payload():
    payload = {
        "uploads": [
            {"file": "video.mp4", "key": "tracks/x/1/staging/video.mp4", "sha256": "ab", "size_bytes": 7},
            {"file": "stems/bass.m4a", "key": "k", "sha256": "cd", "size_bytes": 9},
        ]
    }
    objs = StagedObject.from_result_payload(payload)
    assert [o.file for o in objs] == ["video.mp4", "stems/bass.m4a"]
    assert objs[0].size_bytes == 7 and objs[0].sha256 == "ab"


# --- verification -------------------------------------------------------------


def test_verify_passes_and_streams_sha256_when_s3_stores_none(s3):
    reported = _stage(s3)
    report = Publisher(s3, BUCKET).verify_staging(TRACK_ID, GENERATION, reported)
    assert report.ok
    assert report.sha256_verified == len(STAGED_FILES)
    assert {o.sha256_source for o in report.objects} == {"streamed"}
    assert report.total_bytes == sum(len(v) for v in STAGED_FILES.values())


def test_verify_uses_stored_checksum_without_downloading(s3):
    reported = _stage(s3, with_checksums=True)
    rec = RecordingS3(s3)
    report = Publisher(rec, BUCKET).verify_staging(TRACK_ID, GENERATION, reported)
    assert report.ok
    assert {o.sha256_source for o in report.objects} == {"s3-checksum"}
    assert "get_object" not in rec.names(), "stored checksum path must not download bytes"


def test_composite_multipart_sha256_is_not_compared_as_a_full_object_digest():
    assert (
        _stored_sha256(
            {
                "ChecksumType": "COMPOSITE",
                "ChecksumSHA256": "xEwkeB/modQiZz65/Pg9C80AfgO5Y/udK3ak90iOBRQ=-11",
            }
        )
        is None
    )
    assert _stored_sha256({"ChecksumType": "FULL_OBJECT", "ChecksumSHA256": "YWJj"}) == "616263"


def test_verify_missing_object_raises_checksum_mismatch(s3):
    reported = _stage(s3)
    s3.delete_object(
        Bucket=BUCKET, Key=f"{staging_prefix(TRACK_ID, GENERATION)}stems/drums.m4a"
    )
    with pytest.raises(StagedObjectMissing) as exc:
        Publisher(s3, BUCKET).verify_staging(TRACK_ID, GENERATION, reported)
    assert exc.value.code is ErrorCode.CHECKSUM_MISMATCH
    assert exc.value.to_stage_error().code is ErrorCode.CHECKSUM_MISMATCH
    assert exc.value.to_stage_error().retryable is False
    assert [f.file for f in exc.value.failures] == ["stems/drums.m4a"]


def test_verify_size_mismatch_raises(s3):
    reported = _stage(s3)
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{staging_prefix(TRACK_ID, GENERATION)}video.mp4",
        Body=b"truncated",
    )
    with pytest.raises(ChecksumMismatch) as exc:
        Publisher(s3, BUCKET).verify_staging(TRACK_ID, GENERATION, reported)
    assert "size mismatch" in str(exc.value)
    assert exc.value.code is ErrorCode.CHECKSUM_MISMATCH


def test_verify_corrupted_bytes_same_size_raises(s3):
    """The case size alone cannot catch: same length, different content."""
    reported = _stage(s3)
    original = STAGED_FILES["stems/vocals.m4a"]
    corrupted = b"X" * len(original)
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{staging_prefix(TRACK_ID, GENERATION)}stems/vocals.m4a",
        Body=corrupted,
    )
    with pytest.raises(ChecksumMismatch) as exc:
        Publisher(s3, BUCKET).verify_staging(TRACK_ID, GENERATION, reported)
    assert "sha256 mismatch" in str(exc.value)
    assert exc.value.code is ErrorCode.CHECKSUM_MISMATCH


def test_size_only_policy_records_unverified_and_misses_corruption(s3):
    reported = _stage(s3)
    original = STAGED_FILES["stems/vocals.m4a"]
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{staging_prefix(TRACK_ID, GENERATION)}stems/vocals.m4a",
        Body=b"X" * len(original),
    )
    report = Publisher(s3, BUCKET, checksum_policy=ChecksumPolicy.SIZE_ONLY).verify_staging(
        TRACK_ID, GENERATION, reported
    )
    assert report.ok
    assert report.sha256_verified == 0
    assert report.to_integrity()["sha256_unverified"] == len(STAGED_FILES)


def test_stored_policy_fails_when_no_checksum_present(s3):
    reported = _stage(s3)  # no ChecksumAlgorithm on put
    with pytest.raises(ChecksumMismatch) as exc:
        Publisher(s3, BUCKET, checksum_policy=ChecksumPolicy.STORED).verify_staging(
            TRACK_ID, GENERATION, reported
        )
    assert "no stored ChecksumSHA256" in str(exc.value)


def test_verify_reports_but_tolerates_extra_staged_objects(s3):
    reported = _stage(s3)
    s3.put_object(
        Bucket=BUCKET, Key=f"{staging_prefix(TRACK_ID, GENERATION)}scratch.tmp", Body=b"junk"
    )
    report = Publisher(s3, BUCKET).verify_staging(TRACK_ID, GENERATION, reported)
    assert report.ok
    assert report.extra_staged_keys == ["scratch.tmp"]
    assert report.to_integrity()["extra_staged_keys"] == ["scratch.tmp"]


def test_verify_empty_report_raises(s3):
    with pytest.raises(ChecksumMismatch):
        Publisher(s3, BUCKET).verify_staging(TRACK_ID, GENERATION, [])


# --- promotion / ordering -----------------------------------------------------


def test_promote_uses_server_side_copy_and_writes_manifest_last(s3):
    reported = _stage(s3)
    rec = RecordingS3(s3)
    result = Publisher(rec, BUCKET).publish(TRACK_ID, GENERATION, reported)

    copied = rec.copied_keys()
    assert len(copied) == len(STAGED_FILES)
    assert copied[-1] == manifest_key(TRACK_ID, GENERATION), "manifest must be written LAST"
    assert all(k != manifest_key(TRACK_ID, GENERATION) for k in copied[:-1])

    # Server-side copy only: nothing uploaded, nothing downloaded during promotion.
    assert "put_object" not in rec.names()
    assert "upload_file" not in rec.names()

    # Every object landed at the immutable prefix with identical bytes.
    for rel, data in STAGED_FILES.items():
        got = s3.get_object(Bucket=BUCKET, Key=f"{generation_prefix(TRACK_ID, GENERATION)}{rel}")
        assert got["Body"].read() == data

    assert result.bytes_copied == sum(len(v) for v in STAGED_FILES.values())
    assert result.manifest_key == manifest_key(TRACK_ID, GENERATION)
    assert result.s3_prefix == f"tracks/{TRACK_ID}/{GENERATION}"
    assert result.already_published is False


def test_manifest_absent_from_destination_until_every_object_copied(s3):
    """A crash mid-promotion must leave the generation manifest-less."""
    reported = _stage(s3)
    gen = generation_prefix(TRACK_ID, GENERATION)
    seen_at_each_copy: list[bool] = []

    class FailingS3(RecordingS3):
        def copy_object(self, **kwargs):
            # Record whether the manifest exists at the destination *before*
            # this copy, then fail on the third media object.
            listed = self._inner.list_objects_v2(Bucket=BUCKET, Prefix=gen)
            keys = {o["Key"] for o in listed.get("Contents", [])}
            seen_at_each_copy.append(f"{gen}{MANIFEST_NAME}" in keys)
            if len(seen_at_each_copy) == 3:
                raise RuntimeError("simulated network death")
            return self._inner.copy_object(**kwargs)

    failing = FailingS3(s3)
    with pytest.raises(PublishError):
        Publisher(failing, BUCKET).publish(TRACK_ID, GENERATION, reported)

    assert not any(seen_at_each_copy), "manifest appeared before promotion finished"
    listed = s3.list_objects_v2(Bucket=BUCKET, Prefix=gen)
    keys = {o["Key"] for o in listed.get("Contents", []) if "/staging/" not in o["Key"]}
    assert f"{gen}{MANIFEST_NAME}" not in keys
    assert Publisher(s3, BUCKET).is_published(TRACK_ID, GENERATION) is False


def test_promote_refuses_a_staged_set_with_no_manifest(s3):
    files = {k: v for k, v in STAGED_FILES.items() if k != MANIFEST_NAME}
    reported = _stage(s3, files)
    with pytest.raises(PublishError, match="no manifest.json"):
        Publisher(s3, BUCKET).publish(TRACK_ID, GENERATION, reported)


def test_multipart_copy_used_above_the_single_copy_limit(s3):
    reported = _stage(s3)
    rec = RecordingS3(s3)
    # 20-byte limit forces every object through the multipart path.
    pub = Publisher(rec, BUCKET, single_copy_limit=20, copy_part_size=5 * 1024 * 1024)
    pub.publish(TRACK_ID, GENERATION, reported)
    assert "upload_part_copy" in rec.names()
    for rel, data in STAGED_FILES.items():
        got = s3.get_object(Bucket=BUCKET, Key=f"{generation_prefix(TRACK_ID, GENERATION)}{rel}")
        assert got["Body"].read() == data


# --- immutability / idempotency ----------------------------------------------


def test_published_generation_is_never_overwritten(s3):
    reported = _stage(s3)
    pub = Publisher(s3, BUCKET)
    pub.publish(TRACK_ID, GENERATION, reported)

    # Someone re-stages different bytes and re-runs the publisher.
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{staging_prefix(TRACK_ID, GENERATION)}video.mp4",
        Body=b"DIFFERENT",
    )
    rec = RecordingS3(s3)
    second = Publisher(rec, BUCKET).publish(TRACK_ID, GENERATION, reported)

    assert second.already_published is True
    assert rec.copied_keys() == []
    got = s3.get_object(Bucket=BUCKET, Key=f"{generation_prefix(TRACK_ID, GENERATION)}video.mp4")
    assert got["Body"].read() == STAGED_FILES["video.mp4"]


def test_new_generation_publishes_alongside_the_old(s3):
    reported = _stage(s3)
    Publisher(s3, BUCKET).publish(TRACK_ID, GENERATION, reported)

    prefix2 = staging_prefix(TRACK_ID, 2)
    reported2 = []
    for rel, data in STAGED_FILES.items():
        body = data + b"-v2"
        s3.put_object(Bucket=BUCKET, Key=f"{prefix2}{rel}", Body=body)
        reported2.append(StagedObject(file=rel, size_bytes=len(body), sha256=_sha(body)))
    Publisher(s3, BUCKET).publish(TRACK_ID, 2, reported2)

    g1 = s3.get_object(Bucket=BUCKET, Key=f"{generation_prefix(TRACK_ID, 1)}video.mp4")
    g2 = s3.get_object(Bucket=BUCKET, Key=f"{generation_prefix(TRACK_ID, 2)}video.mp4")
    assert g1["Body"].read() == STAGED_FILES["video.mp4"]
    assert g2["Body"].read() == STAGED_FILES["video.mp4"] + b"-v2"


# --- DB seam ------------------------------------------------------------------


async def _advance_to_publishing(job_repo, job):
    await job_repo.advance(job.id, from_stage=JobStage.pending, to_stage=JobStage.downloading)
    await job_repo.advance(job.id, from_stage=JobStage.downloading, to_stage=JobStage.splitting)
    await job_repo.advance(job.id, from_stage=JobStage.splitting, to_stage=JobStage.verifying)
    return await job_repo.advance(
        job.id, from_stage=JobStage.verifying, to_stage=JobStage.publishing
    )


async def test_publish_job_writes_track_row_and_is_idempotent(s3, job_repo, upload_job):
    job = await _advance_to_publishing(job_repo, upload_job)
    track_id = track_id_for_job(job.id)

    prefix = staging_prefix(track_id, 1)
    uploads = []
    for rel, data in STAGED_FILES.items():
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}{rel}", Body=data)
        uploads.append({"file": rel, "sha256": _sha(data), "size_bytes": len(data)})
    worker_result = {"uploads": uploads, "integrity": {"profile_version": 1, "gate_a": {"passed": True}}}

    pub = Publisher(s3, BUCKET)
    track, result = await publish_job(
        jobs=job_repo,
        publisher=pub,
        job=job,
        worker_result=worker_result,
        manifest=MANIFEST,
    )
    assert track.id == track_id
    assert track.title == "Test Track"
    assert track.duration_seconds == pytest.approx(123.5)
    assert track.s3_prefix == f"tracks/{track_id}/1"
    assert track.manifest_key == f"tracks/{track_id}/1/manifest.json"
    assert track.integrity["publisher"]["object_count"] == len(STAGED_FILES)
    assert track.integrity["worker"]["profile_version"] == 1

    refreshed = await job_repo.get_job(job.id)
    assert refreshed.status is JobStage.ready
    assert refreshed.track_id == track_id

    # Re-run (duplicate completion): same row, no re-copy, no error.
    rec = RecordingS3(s3)
    track2, result2 = await publish_job(
        jobs=job_repo,
        publisher=Publisher(rec, BUCKET),
        job=refreshed,
        worker_result=worker_result,
        manifest=MANIFEST,
    )
    assert track2.id == track.id
    assert result2.already_published is True
    assert rec.copied_keys() == []
    assert result.already_published is False


async def test_publish_job_falls_back_to_job_artist(s3, job_repo, settings):
    """When the manifest carries no artist, the track inherits the job's."""
    from shizzle_server.db.models import SourceType

    job_id = uuid.uuid4()
    job_dir = settings.data_dir / job_id.hex
    job_dir.mkdir(parents=True)
    (job_dir / "source.mp4").write_bytes(b"fake video bytes")
    job = await job_repo.create_job(
        job_id=job_id,
        source_type=SourceType.upload,
        source_ref="source.mp4",
        title="Spoonman",
        artist="Soundgarden",
    )
    job = await _advance_to_publishing(job_repo, job)
    track_id = track_id_for_job(job.id)

    prefix = staging_prefix(track_id, 1)
    uploads = []
    for rel, data in STAGED_FILES.items():
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}{rel}", Body=data)
        uploads.append({"file": rel, "sha256": _sha(data), "size_bytes": len(data)})

    manifest = {k: v for k, v in MANIFEST.items() if k != "artist"}
    track, _ = await publish_job(
        jobs=job_repo,
        publisher=Publisher(s3, BUCKET),
        job=job,
        worker_result={"uploads": uploads},
        manifest=manifest,
    )
    assert track.artist == "Soundgarden"
    assert track.title == "Test Track"  # manifest title still wins

    # An empty-string manifest artist falls back the same way.
    track2, _ = await publish_job(
        jobs=job_repo,
        publisher=Publisher(s3, BUCKET),
        job=await job_repo.get_job(job.id),
        worker_result={"uploads": uploads},
        manifest={**MANIFEST, "artist": ""},
    )
    assert track2.artist == "Soundgarden"


async def test_publish_job_maps_checksum_mismatch_to_stage_error(s3, job_repo, upload_job):
    from shizzle_server.errors import StageError

    job = await _advance_to_publishing(job_repo, upload_job)
    track_id = track_id_for_job(job.id)
    prefix = staging_prefix(track_id, 1)
    uploads = []
    for rel, data in STAGED_FILES.items():
        s3.put_object(Bucket=BUCKET, Key=f"{prefix}{rel}", Body=data)
        uploads.append({"file": rel, "sha256": _sha(data), "size_bytes": len(data)})
    # Corrupt one staged object in place, same length.
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{prefix}stems/drums.m4a",
        Body=b"Z" * len(STAGED_FILES["stems/drums.m4a"]),
    )

    with pytest.raises(StageError) as exc:
        await publish_job(
            jobs=job_repo,
            publisher=Publisher(s3, BUCKET),
            job=job,
            worker_result={"uploads": uploads},
            manifest=MANIFEST,
        )
    assert exc.value.code is ErrorCode.CHECKSUM_MISMATCH
    assert exc.value.retryable is False

    # Nothing was promoted and the job is still in publishing (loop will retry/fail it).
    assert Publisher(s3, BUCKET).is_published(track_id, 1) is False
    refreshed = await job_repo.get_job(job.id)
    assert refreshed.status is JobStage.publishing


def test_copy_object_preserves_content_type_by_default_and_overrides_on_request(s3):
    key = "src/clip.mp4"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"bytes", ContentType="binary/octet-stream")
    pub = Publisher(s3, BUCKET)

    pub.copy_object(key, "dst/preserved.mp4", 5)
    assert (
        s3.head_object(Bucket=BUCKET, Key="dst/preserved.mp4")["ContentType"]
        == "binary/octet-stream"
    )

    pub.copy_object(key, "dst/fixed.mp4", 5, content_type="video/mp4")
    assert s3.head_object(Bucket=BUCKET, Key="dst/fixed.mp4")["ContentType"] == "video/mp4"


def test_multipart_copy_sets_content_type(s3):
    key = "src/big.wav"
    s3.put_object(Bucket=BUCKET, Key=key, Body=b"x" * 4096, ContentType="binary/octet-stream")
    pub = Publisher(s3, BUCKET, single_copy_limit=10, copy_part_size=5 * 1024 * 1024)
    pub.copy_object(key, "dst/big.wav", 4096, content_type="audio/wav")
    head = s3.head_object(Bucket=BUCKET, Key="dst/big.wav")
    assert head["ContentType"] == "audio/wav"
    assert head["ContentLength"] == 4096


async def test_upsert_imported_is_idempotent_and_undeletes(track_repo):
    from shizzle_server.db.repository import track_id_for_import

    tid = track_id_for_import("karaoke/pub/07589117e9ab")
    track, created = await track_repo.upsert_imported(
        tid,
        title="Pretty Woman",
        duration_seconds=273.707,
        s3_prefix=f"tracks/{tid}/1",
        manifest_key=f"tracks/{tid}/1/manifest.json",
        integrity={"source": "legacy-import", "gates": "not-run"},
    )
    assert created is True and track.id == tid

    await track_repo.soft_delete(tid)
    assert await track_repo.list_tracks() == []

    track2, created2 = await track_repo.upsert_imported(
        tid,
        title="Pretty Woman (retitled)",
        artist="Van Halen",
        duration_seconds=273.707,
        s3_prefix=f"tracks/{tid}/1",
        manifest_key=f"tracks/{tid}/1/manifest.json",
    )
    assert created2 is False
    assert track2.id == tid
    assert track2.title == "Pretty Woman (retitled)"
    assert track2.artist == "Van Halen"
    live = await track_repo.list_tracks()
    assert [t.id for t in live] == [tid], "re-import must converge, not duplicate"


def test_track_id_for_import_is_deterministic_and_distinct_from_job_ids():
    from shizzle_server.db.repository import track_id_for_import

    a = track_id_for_import("07589117e9ab")
    assert a == track_id_for_import("07589117e9ab")
    assert a != track_id_for_import("07589117e9ac")
    assert a != track_id_for_job(uuid.UUID("07589117-e9ab-4000-8000-000000000000"))


def test_object_verification_as_dict_carries_failure_reason():
    ov = ObjectVerification(
        file="video.mp4", staging_key="k", expected_size=1, reason="missing from staging prefix"
    )
    assert ov.as_dict()["ok"] is False
    assert ov.as_dict()["reason"] == "missing from staging prefix"


# --- stem format guard --------------------------------------------------------
# Regression guard for "The Pot" (2026-08-04): one track was imported with
# 6 x 95 MiB raw WAV stems and froze the player. Raw/uncompressed or absurdly
# large stems must never be publishable or importable again.


def test_validate_stem_object_accepts_normal_m4a():
    assert validate_stem_object("stems/vocals.m4a", 12 * 1024**2) is None


def test_validate_stem_object_rejects_wav():
    reason = validate_stem_object("stems/vocals.wav", 1024)
    assert reason is not None and ".wav" in reason


def test_validate_stem_object_rejects_oversized_m4a():
    reason = validate_stem_object("stems/drums.m4a", MAX_STEM_BYTES + 1)
    assert reason is not None and "cap" in reason


def test_stem_cap_covers_maximum_upload_duration_at_fixed_bitrate():
    encoded_bytes = 256_000 / 8 * (30 * 60)
    assert encoded_bytes * 1.05 <= MAX_STEM_BYTES


def test_validate_stem_object_ignores_non_stem_objects():
    # Video/multitrack/manifest are not stems; the WAV-era 573 MB failure mode
    # was specifically per-stem audio.
    assert validate_stem_object("video.mp4", 500 * 1024**2) is None
    assert validate_stem_object("multi-track.mp4", 500 * 1024**2) is None
    assert validate_stem_object(MANIFEST_NAME, 1024) is None


def test_validate_stem_object_handles_unknown_size():
    # Size unknown: format still enforced, cap cannot be.
    assert validate_stem_object("stems/bass.m4a", None) is None
    assert validate_stem_object("stems/bass.wav", None) is not None


def test_validate_stem_objects_raises_with_every_problem_listed():
    with pytest.raises(InvalidStemObject) as exc:
        validate_stem_objects(
            [
                ("stems/vocals.wav", 100 * 1024**2),
                ("stems/drums.m4a", MAX_STEM_BYTES + 1),
                ("video.mp4", 500 * 1024**2),
            ]
        )
    message = str(exc.value)
    assert "2 stem object(s) rejected" in message
    assert "stems/vocals.wav" in message and "stems/drums.m4a" in message
    assert exc.value.retryable is False


def test_publish_refuses_wav_stems_and_promotes_nothing(s3):
    files = dict(STAGED_FILES)
    files["stems/vocals.wav"] = b"RIFF" + b"\x00" * 128
    reported = _stage(s3, files)

    rec = RecordingS3(s3)
    with pytest.raises(InvalidStemObject):
        Publisher(rec, BUCKET).publish(TRACK_ID, GENERATION, reported)

    # Nothing was promoted: no copies happened and the destination manifest
    # (the generation-complete marker) does not exist.
    assert rec.copied_keys() == []
    assert Publisher(s3, BUCKET).is_published(TRACK_ID, GENERATION) is False


def test_publish_refuses_oversized_stem(s3):
    reported = _stage(s3)
    # The worker reports a stem whose size is over the cap (moto holds the
    # small placeholder bytes; the guard trusts the reported size).
    reported = [
        StagedObject(file=o.file, size_bytes=MAX_STEM_BYTES + 1, sha256=o.sha256)
        if o.file == "stems/drums.m4a"
        else o
        for o in reported
    ]
    with pytest.raises(InvalidStemObject):
        Publisher(s3, BUCKET).publish(TRACK_ID, GENERATION, reported)
    assert Publisher(s3, BUCKET).is_published(TRACK_ID, GENERATION) is False

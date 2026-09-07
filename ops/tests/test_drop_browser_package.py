"""Tests for ops/drop_browser_package.py (moto-backed S3).

The client-side contract: local bytes are verified before any upload, media
lands first and manifest.json LAST, and a restart skips objects whose remote
size already matches.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import uuid
from pathlib import Path
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

MODULE_PATH = Path(__file__).resolve().parents[1] / "drop_browser_package.py"
SPEC = importlib.util.spec_from_file_location("drop_browser_package", MODULE_PATH)
assert SPEC and SPEC.loader
drop = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = drop
SPEC.loader.exec_module(drop)

BUCKET = "shizzle-drop-test"
ROLES = ("vocals", "drums", "bass", "guitar", "piano", "shizzle")
MEDIA_FILES = [f"stems/{role}.m4a" for role in ROLES] + ["video.mp4"]


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
def s3(fake_aws_credentials):  # noqa: ARG001
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


class RecordingS3:
    """Thin proxy recording the ordered upload/head calls the dropper makes."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple[str, str]] = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr) or name == "get_paginator":
            return attr

        def wrapped(*args, **kwargs):
            key = kwargs.get("Key") or (args[-1] if args else "")
            self.calls.append((name, str(key)))
            return attr(*args, **kwargs)

        return wrapped

    def get_paginator(self, name):
        return self._inner.get_paginator(name)

    def uploaded_keys(self) -> list[str]:
        return [key for name, key in self.calls if name == "put_object"]


def _make_candidate(root: Path) -> tuple[Path, dict[str, bytes]]:
    media = {file: f"drop-bytes:{file}".encode() * 4 for file in MEDIA_FILES}
    for file, data in media.items():
        target = root / file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    manifest = {
        "version": 3,
        "delivery_profile": "shizzle-browser-v1",
        "title": "Client Drop",
        "artist": "Client",
        "duration": 10.0,
        "video": "video.mp4",
        "stems": [
            {"id": role, "file": f"stems/{role}.m4a", "default_gain_db": 0.0} for role in ROLES
        ],
        "timeline": {"start_ms": 0, "duration_ms": 10_000, "sample_rate_hz": 44100},
        "integrity": {
            "objects": [
                {
                    "file": file,
                    "bytes": len(media[file]),
                    "sha256": hashlib.sha256(media[file]).hexdigest(),
                }
                for file in sorted(media)
            ]
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    return root, media


def _run(s3, monkeypatch, candidate: Path, ref: str) -> RecordingS3:
    rec = RecordingS3(s3)
    monkeypatch.setattr(drop.boto3, "client", lambda *_args, **_kwargs: rec)
    code = drop.main(["--candidate", str(candidate), "--source-ref", ref, "--bucket", BUCKET])
    assert code == 0
    return rec


def test_manifest_is_uploaded_last(s3, monkeypatch, tmp_path):
    candidate, _media = _make_candidate(tmp_path / "candidate")
    ref = f"youtube-{uuid.uuid4().hex[:8]}"

    rec = _run(s3, monkeypatch, candidate, ref)

    uploaded = rec.uploaded_keys()
    assert uploaded[-1].endswith("manifest.json"), "manifest.json must be the final upload"
    assert {key[len(f"imports/{ref}/"):] for key in uploaded} == {*MEDIA_FILES, "manifest.json"}


def test_restart_skips_byte_identical_objects_and_reuploads_changed_content(
    s3, monkeypatch, tmp_path
):
    candidate, media = _make_candidate(tmp_path / "candidate")
    ref = f"sha256-{uuid.uuid4().hex[:16]}"
    prefix = f"imports/{ref}/"
    # A previous run got three stems up: two byte-identical, one the SAME
    # LENGTH but different content.
    s3.put_object(Bucket=BUCKET, Key=f"{prefix}stems/vocals.m4a", Body=media["stems/vocals.m4a"])
    s3.put_object(Bucket=BUCKET, Key=f"{prefix}stems/drums.m4a", Body=media["stems/drums.m4a"])
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{prefix}stems/bass.m4a",
        Body=b"x" * len(media["stems/bass.m4a"]),  # same size, wrong bytes
    )

    rec = _run(s3, monkeypatch, candidate, ref)

    uploaded = {key[len(prefix):] for key in rec.uploaded_keys()}
    assert {"stems/vocals.m4a", "stems/drums.m4a"}.isdisjoint(uploaded)  # identical: skipped
    assert "stems/bass.m4a" in uploaded  # R8: same-length corruption is re-uploaded
    assert set(MEDIA_FILES[3:]) | {"manifest.json"} <= uploaded


def test_local_sha_mismatch_aborts_before_any_upload(s3, monkeypatch, tmp_path, capsys):
    candidate, _media = _make_candidate(tmp_path / "candidate")
    # Corrupt one local stem after the manifest was written.
    (candidate / "stems" / "vocals.m4a").write_bytes(b"tampered")

    rec = RecordingS3(s3)
    monkeypatch.setattr(drop.boto3, "client", lambda *_args, **_kwargs: rec)
    ref = f"youtube-{uuid.uuid4().hex[:8]}"
    code = drop.main(["--candidate", str(candidate), "--source-ref", ref, "--bucket", BUCKET])

    assert code == 2
    assert rec.calls == [], "no S3 call may happen after a local mismatch"
    payload: dict[str, Any] = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "candidate-mismatch"


def test_manifest_put_is_final_even_when_remote_size_matches(s3, monkeypatch, tmp_path):
    candidate, _media = _make_candidate(tmp_path / "candidate")
    ref = f"sha256-{uuid.uuid4().hex[:16]}"
    prefix = f"imports/{ref}/"
    # Remote already holds a same-size manifest and one changed media object.
    s3.put_object(
        Bucket=BUCKET, Key=f"{prefix}manifest.json", Body=(candidate / "manifest.json").read_bytes()
    )
    s3.put_object(Bucket=BUCKET, Key=f"{prefix}stems/vocals.m4a", Body=b"changed-media")

    rec = _run(s3, monkeypatch, candidate, ref)

    uploads = [key for name, key in rec.calls if name == "put_object"]
    assert uploads[-1] == f"{prefix}manifest.json"  # manifest put is the FINAL put
    assert f"{prefix}stems/vocals.m4a" in uploads  # changed media re-uploaded


def _mutate_invalid(manifest: dict, case: str) -> None:
    objects = manifest["integrity"]["objects"]
    if case == "empty-objects":
        manifest["integrity"]["objects"] = []
    elif case == "missing-file":
        objects.pop(0)
    elif case == "extra-file":
        objects.append({"file": "extra.bin", "bytes": 5, "sha256": "0" * 64})
    elif case == "duplicate-file":
        objects.append(dict(objects[0]))
    elif case == "malformed-sha":
        objects[0]["sha256"] = "NOTHEX" * 8
    elif case == "bad-bytes":
        objects[0]["bytes"] = 0
    else:
        raise AssertionError(f"unknown case {case}")


@pytest.mark.parametrize(
    "case",
    ["empty-objects", "missing-file", "extra-file", "duplicate-file", "malformed-sha", "bad-bytes"],
)
def test_invalid_manifest_structures_refuse_before_any_upload(
    s3, monkeypatch, tmp_path, capsys, case
):
    candidate, _media = _make_candidate(tmp_path / "candidate")
    manifest = json.loads((candidate / "manifest.json").read_text())
    _mutate_invalid(manifest, case)
    (candidate / "manifest.json").write_text(json.dumps(manifest))

    rec = RecordingS3(s3)
    monkeypatch.setattr(drop.boto3, "client", lambda *_args, **_kwargs: rec)
    ref = f"youtube-{uuid.uuid4().hex[:8]}"
    code = drop.main(["--candidate", str(candidate), "--source-ref", ref, "--bucket", BUCKET])

    assert code == 2
    assert rec.calls == [], "structural problems must not reach S3"
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "invalid-manifest"
    assert payload["message"]


def test_non_object_manifest_refuses_before_any_upload(s3, monkeypatch, tmp_path, capsys):
    candidate, _media = _make_candidate(tmp_path / "candidate")
    (candidate / "manifest.json").write_text("[1, 2, 3]")

    rec = RecordingS3(s3)
    monkeypatch.setattr(drop.boto3, "client", lambda *_args, **_kwargs: rec)
    ref = f"youtube-{uuid.uuid4().hex[:8]}"
    code = drop.main(["--candidate", str(candidate), "--source-ref", ref, "--bucket", BUCKET])

    assert code == 2
    assert rec.calls == []
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "invalid-manifest"


def test_malformed_json_manifest_refuses_before_any_upload(s3, monkeypatch, tmp_path, capsys):
    candidate, _media = _make_candidate(tmp_path / "candidate")
    (candidate / "manifest.json").write_text("not json{")

    rec = RecordingS3(s3)
    monkeypatch.setattr(drop.boto3, "client", lambda *_args, **_kwargs: rec)
    ref = f"youtube-{uuid.uuid4().hex[:8]}"
    code = drop.main(["--candidate", str(candidate), "--source-ref", ref, "--bucket", BUCKET])

    assert code == 2
    assert rec.calls == []
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "invalid-manifest"


def test_client_init_failure_prints_one_json_line_and_fails(monkeypatch, tmp_path, capsys):
    candidate, _media = _make_candidate(tmp_path / "candidate")

    def boom(*_args, **_kwargs):
        raise RuntimeError("no usable credentials")

    monkeypatch.setattr(drop.boto3, "client", boom)
    ref = f"youtube-{uuid.uuid4().hex[:8]}"
    code = drop.main(["--candidate", str(candidate), "--source-ref", ref, "--bucket", BUCKET])

    assert code == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "client-failed"


def test_invalid_source_ref_refused_without_client(monkeypatch, tmp_path):
    candidate, _media = _make_candidate(tmp_path / "candidate")

    def explode(*_a, **_k):
        raise AssertionError("client must not be created for an invalid ref")

    monkeypatch.setattr(drop.boto3, "client", explode)
    code = drop.main(
        ["--candidate", str(candidate), "--source-ref", "karaoke/pub/x", "--bucket", BUCKET]
    )
    assert code == 2


# --- --wait receipt correlation (R7/R9/R21) ------------------------------------


def _manifest_sha(candidate: Path) -> str:
    return hashlib.sha256((candidate / "manifest.json").read_bytes()).hexdigest()


def test_wait_returns_fresh_matching_receipt_and_ignores_stale(
    s3, monkeypatch, tmp_path, capsys
):
    candidate, _media = _make_candidate(tmp_path / "candidate")
    ref = f"youtube-{uuid.uuid4().hex[:8]}"
    result_key = f"imports/{ref}/result.json"
    # A stale receipt from a PREVIOUS drop of different content.
    s3.put_object(
        Bucket=BUCKET,
        Key=result_key,
        Body=json.dumps({"status": "published", "manifestSha256": "0" * 64}).encode(),
    )
    fresh = {
        "status": "published",
        "manifestSha256": _manifest_sha(candidate),
    }
    sleeps = {"n": 0}

    def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] == 1:  # the ingest finishes while we are waiting
            s3.put_object(Bucket=BUCKET, Key=result_key, Body=json.dumps(fresh).encode())

    monkeypatch.setattr(drop.time, "sleep", fake_sleep)
    code = drop.main(
        ["--candidate", str(candidate), "--source-ref", ref, "--bucket", BUCKET, "--wait",
         "--wait-seconds", "60"]
    )
    assert code == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload == fresh  # the STALE receipt was never returned


def test_wait_rejected_receipt_exits_2(s3, monkeypatch, tmp_path, capsys):
    candidate, _media = _make_candidate(tmp_path / "candidate")
    ref = f"youtube-{uuid.uuid4().hex[:8]}"
    result_key = f"imports/{ref}/result.json"
    sleeps = {"n": 0}

    def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            s3.put_object(
                Bucket=BUCKET,
                Key=result_key,
                Body=json.dumps(
                    {"status": "rejected", "manifestSha256": _manifest_sha(candidate)}
                ).encode(),
            )

    monkeypatch.setattr(drop.time, "sleep", fake_sleep)
    code = drop.main(
        ["--candidate", str(candidate), "--source-ref", ref, "--bucket", BUCKET, "--wait",
         "--wait-seconds", "60"]
    )
    assert code == 2
    payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert payload["status"] == "rejected"


def test_wait_timeout_exits_1_with_one_line(s3, tmp_path, capsys):
    candidate, _media = _make_candidate(tmp_path / "candidate")
    ref = f"youtube-{uuid.uuid4().hex[:8]}"
    # Only a stale, non-matching receipt exists; the deadline is immediate.
    s3.put_object(
        Bucket=BUCKET,
        Key=f"imports/{ref}/result.json",
        Body=json.dumps({"status": "published", "manifestSha256": "0" * 64}).encode(),
    )
    code = drop.main(
        ["--candidate", str(candidate), "--source-ref", ref, "--bucket", BUCKET, "--wait",
         "--wait-seconds", "0"]
    )
    assert code == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == "wait-timeout"


class _FailingPollS3(RecordingS3):
    """head_object on result.json denies once the uploads are done (R9)."""

    def __init__(self, inner):
        super().__init__(inner)
        self.puts = 0

    def put_object(self, *args, **kwargs):
        self.calls.append(("put_object", str(kwargs.get("Key", ""))))
        self.puts += 1
        return self._inner.put_object(*args, **kwargs)

    def head_object(self, *args, **kwargs):
        if str(kwargs.get("Key", "")).endswith("result.json") and self.puts >= 8:
            raise ClientError(
                {"Error": {"Code": "403", "Message": "AccessDenied"}}, "HeadObject"
            )
        self.calls.append(("head_object", str(kwargs.get("Key", ""))))
        return self._inner.head_object(*args, **kwargs)


def test_wait_poll_failure_fails_fast_one_line(s3, monkeypatch, tmp_path, capsys):
    candidate, _media = _make_candidate(tmp_path / "candidate")
    ref = f"youtube-{uuid.uuid4().hex[:8]}"
    rec = _FailingPollS3(s3)
    monkeypatch.setattr(drop.boto3, "client", lambda *_args, **_kwargs: rec)
    code = drop.main(
        ["--candidate", str(candidate), "--source-ref", ref, "--bucket", BUCKET, "--wait",
         "--wait-seconds", "60"]
    )
    assert code == 1
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "wait-failed"
    assert payload["error"]["code"] == "403"


class _FailingUploadS3(RecordingS3):
    def put_object(self, *_args, **kwargs):
        self.calls.append(("put_object", str(kwargs.get("Key", ""))))
        raise RuntimeError("put failed via http://user:SUPERSECRET@proxy.example")


def test_upload_failure_is_sanitized_and_never_leaks(s3, monkeypatch, tmp_path, capsys):
    candidate, _media = _make_candidate(tmp_path / "candidate")
    rec = _FailingUploadS3(s3)
    monkeypatch.setattr(drop.boto3, "client", lambda *_args, **_kwargs: rec)
    ref = f"youtube-{uuid.uuid4().hex[:8]}"
    code = drop.main(["--candidate", str(candidate), "--source-ref", ref, "--bucket", BUCKET])
    assert code == 1
    captured = capsys.readouterr()
    assert "SUPERSECRET" not in captured.out + captured.err
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["status"] == "upload-failed"
    assert payload["error"]["type"] == "RuntimeError"
    assert payload["error"]["message"] == "operation failed"


def test_fetch_receipt_treats_get_race_and_bad_utf8_as_not_yet(s3, monkeypatch):
    """R9: a receipt that vanishes between HEAD and GET, or whose body is not
    valid UTF-8, is a 'not yet' condition — the poll keeps waiting."""
    from botocore.exceptions import ClientError

    key = "imports/youtube-fetchrace001/result.json"
    s3.put_object(Bucket=BUCKET, Key=key, Body=bytes([0xFF, 0xFE]) + b" not json")
    assert drop._fetch_receipt(s3, BUCKET, key, stale_etag=None) is None  # bad UTF-8

    real_get = s3.get_object

    def racing_get(**_kwargs):
        raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "gone"}}, "GetObject")

    monkeypatch.setattr(s3, "get_object", racing_get)
    assert drop._fetch_receipt(s3, BUCKET, key, stale_etag=None) is None  # GET raced to 404
    monkeypatch.setattr(s3, "get_object", real_get)

    def denied_get(**_kwargs):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetObject")

    monkeypatch.setattr(s3, "get_object", denied_get)
    with pytest.raises(ClientError):  # anything else still fails fast
        drop._fetch_receipt(s3, BUCKET, key, stale_etag=None)

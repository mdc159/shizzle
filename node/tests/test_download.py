"""Download executor pieces that need no network: probe validation, error
classification, the S3 upload order (source.mp4, verify, source.json LAST)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path

import pytest

from shizzle_node.download import (
    DownloadError,
    SourceFile,
    classify_ytdlp_error,
    download_source,
    sha256_file,
    upload_source,
    validate_probe,
    youtube_video_id,
)


def _probe(duration="255.86", fmt="mov,mp4,m4a,3gp,3g2,mj2", kinds=("audio", "video")):
    return {
        "format": {"duration": duration, "format_name": fmt},
        "streams": [{"codec_type": k} for k in kinds],
    }


def test_validate_probe_accepts_a_real_mp4_and_rejects_the_rest():
    assert validate_probe(_probe(), max_duration_seconds=1800) == pytest.approx(255.86)
    with pytest.raises(DownloadError, match="SOURCE_TOO_LONG"):
        validate_probe(_probe(duration="1801"), max_duration_seconds=1800)
    with pytest.raises(DownloadError, match="SOURCE_INVALID"):
        validate_probe(_probe(kinds=("video",)), max_duration_seconds=1800)
    with pytest.raises(DownloadError, match="SOURCE_INVALID"):
        validate_probe(_probe(fmt="matroska,webm"), max_duration_seconds=1800)
    with pytest.raises(DownloadError, match="SOURCE_INVALID"):
        validate_probe(_probe(duration="nan"), max_duration_seconds=1800)


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("ERROR: [youtube] x: Sign in to confirm you're not a bot.", "YOUTUBE_SIGN_IN_REQUIRED"),
        ("ERROR: [youtube] x: Video unavailable", "VIDEO_UNAVAILABLE"),
        ("ERROR: This live event will begin in 3 hours", "LIVE_VIDEO"),
        ("File is larger than max-filesize (2147483648 bytes)", "SOURCE_SIZE"),
        ("ERROR: Unable to download webpage: HTTP Error 503", "DOWNLOAD_FAILED"),
    ],
)
def test_ytdlp_errors_map_to_stable_codes(message, code):
    assert classify_ytdlp_error(message) == code


class _Precondition(RuntimeError):
    def __init__(self) -> None:
        super().__init__("412")
        self.response = {"Error": {"Code": "PreconditionFailed"},
                         "ResponseMetadata": {"HTTPStatusCode": 412}}


class _FakeS3:
    """Honours If-None-Match: a second conditional PUT of an existing key is a 412."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.order: list[str] = []

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch=None, ChecksumSHA256=None):  # noqa: N803, ARG002
        if IfNoneMatch == "*" and Key in self.objects:
            raise _Precondition()
        data = Body.read() if hasattr(Body, "read") else Body
        if ChecksumSHA256 is not None:
            # S3 verifies the upload against the declared digest.
            assert base64.b64decode(ChecksumSHA256) == hashlib.sha256(data).digest()
        self.objects[Key] = data
        self.order.append(f"put:{Key}:{ContentType}:{IfNoneMatch}")

    def head_object(self, *, Bucket, Key):  # noqa: N803, ARG002
        if Key not in self.objects:
            raise RuntimeError("missing")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, *, Bucket, Key):  # noqa: N803, ARG002
        if Key not in self.objects:
            exc = RuntimeError("nope")
            exc.response = {"Error": {"Code": "NoSuchKey"}}  # type: ignore[attr-defined]
            raise exc
        return {"Body": io.BytesIO(self.objects[Key])}


def _source(tmp_path: Path) -> SourceFile:
    path = tmp_path / "source.mp4"
    path.write_bytes(b"0123456789")
    return SourceFile(
        path=path, bytes=10, sha256=sha256_file(path), duration_seconds=1.0, title="t",
        uploader="u", video_id="v", downloader="d",
    )


def test_upload_writes_source_then_receipt_last_conditionally(tmp_path):
    s3 = _FakeS3()
    source = _source(tmp_path)
    receipt = {"track_id": "t", "object_key": "sources/t/source.mp4", "sha256": source.sha256}
    phases: list[str] = []
    landed = upload_source(s3, bucket="b", source=source, receipt=receipt, progress=phases.append)
    assert landed is receipt
    assert s3.order == [
        "put:sources/t/source.mp4:video/mp4:*",
        "put:sources/t/source.json:application/json:*",
    ]
    assert json.loads(s3.objects["sources/t/source.json"])["sha256"] == source.sha256
    assert phases == ["upload: source.mp4", "upload: source.json"]
    # Second run (any sha): the existing receipt is adopted, nothing is rewritten.
    other = {"track_id": "t", "object_key": "sources/t/source.mp4", "sha256": "f" * 64}
    adopted = upload_source(s3, bucket="b", source=source, receipt=other, progress=phases.append)
    assert adopted["sha256"] == source.sha256 and len(s3.order) == 2
    assert phases[-1].startswith("upload: already complete")


def test_stale_node_cannot_overwrite_an_owned_source(tmp_path):
    """First writer wins: a 412 on source.mp4 adopts the owner's receipt."""
    s3 = _FakeS3()
    source = _source(tmp_path)
    s3.objects["sources/t/source.mp4"] = b"the other node's bytes"
    s3.objects["sources/t/source.json"] = json.dumps({"sha256": "a" * 64, "bytes": 22}).encode()
    receipt = {"track_id": "t", "object_key": "sources/t/source.mp4", "sha256": source.sha256}
    adopted = upload_source(s3, bucket="b", source=source, receipt=receipt, progress=lambda _m: None)
    assert adopted == {"sha256": "a" * 64, "bytes": 22}
    assert s3.objects["sources/t/source.mp4"] == b"the other node's bytes"
    assert s3.order == []  # nothing written by the stale node


def test_orphaned_source_without_a_receipt_is_completed_not_overwritten(tmp_path):
    """A node that died between its two writes leaves source.mp4 with no
    source.json. The next node must not fail forever on it: it describes the
    object that is actually there and writes the receipt for it (A12)."""
    s3 = _FakeS3()
    source = _source(tmp_path)
    orphan = b"the dead node's complete upload"
    s3.objects["sources/t/source.mp4"] = orphan
    receipt = {"track_id": "t", "object_key": "sources/t/source.mp4", "sha256": source.sha256,
               "bytes": source.bytes, "title": "t"}
    phases: list[str] = []
    landed = upload_source(s3, bucket="b", source=source, receipt=receipt, progress=phases.append)
    assert s3.objects["sources/t/source.mp4"] == orphan  # never overwritten
    assert s3.order == ["put:sources/t/source.json:application/json:*"]
    assert landed["sha256"] == hashlib.sha256(orphan).hexdigest()
    assert landed["bytes"] == len(orphan) and landed["title"] == "t"
    assert landed["completed_orphan"] is True
    assert json.loads(s3.objects["sources/t/source.json"]) == landed
    assert "upload: source.mp4 exists without a receipt; completing it" in phases
    # The owner, alive after all, writes its receipt next: it adopts ours.
    adopted = upload_source(s3, bucket="b", source=source, receipt=receipt, progress=phases.append)
    assert adopted == landed and len(s3.order) == 1


def test_orphan_that_vanishes_is_a_retryable_race_and_other_errors_propagate(tmp_path):
    source = _source(tmp_path)
    receipt = {"track_id": "t", "object_key": "sources/t/source.mp4", "sha256": source.sha256}

    class Vanishing(_FakeS3):
        """412 on the PUT (owned) but the object is gone by the time we read it."""

        def put_object(self, **kwargs):  # noqa: ANN003
            if kwargs["Key"] == "sources/t/source.mp4":
                raise _Precondition()
            return super().put_object(**kwargs)

    with pytest.raises(DownloadError, match="SOURCE_RACE"):
        upload_source(Vanishing(), bucket="b", source=source, receipt=receipt, progress=lambda _m: None)

    class Forbidden(Vanishing):
        def get_object(self, *, Bucket, Key):  # noqa: N803, ARG002
            if Key.endswith("source.mp4"):
                exc = RuntimeError("403")
                exc.response = {"Error": {"Code": "AccessDenied"}}  # type: ignore[attr-defined]
                raise exc
            return super().get_object(Bucket=Bucket, Key=Key)

    with pytest.raises(RuntimeError, match="403"):  # not disguised as a race
        upload_source(Forbidden(), bucket="b", source=source, receipt=receipt, progress=lambda _m: None)


@pytest.mark.parametrize("hang_on", ["remux", "transcode"])
def test_a_hung_ffmpeg_is_a_structured_failure_not_a_stall(tmp_path, monkeypatch, hang_on):
    import subprocess

    from shizzle_node import download as dl

    source = tmp_path / "source.webm"
    source.write_bytes(b"x")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):  # noqa: ANN001
        calls.append(cmd)
        assert kwargs["timeout"] > 0
        is_remux = "-c" in cmd and cmd[cmd.index("-c") + 1] == "copy"
        if (hang_on == "remux" and is_remux) or (hang_on == "transcode" and not is_remux):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        return subprocess.CompletedProcess(cmd, 1, "", "remux refused")  # remux fails -> transcode

    monkeypatch.setattr(dl.subprocess, "run", fake_run)
    monkeypatch.setattr(dl.shutil, "which", lambda _name: "/usr/bin/ffmpeg")
    with pytest.raises(DownloadError, match="DOWNLOAD_FAILED.*timed out"):
        dl._to_mp4(source, tmp_path / "source.mp4")
    assert len(calls) == (1 if hang_on == "remux" else 2)
    assert source.exists()  # the input is only removed after a successful conversion


@pytest.mark.parametrize(
    "url",
    ["https://vimeo.com/12345", "http://169.254.169.254/latest/meta-data/", "ftp://x/y",
     "https://www.youtube.com/watch?v=short"],
)
def test_download_refuses_anything_but_one_youtube_video(tmp_path, url):
    assert youtube_video_id(url) is None
    with pytest.raises(DownloadError, match="URL_REFUSED"):
        download_source(url, tmp_path, max_bytes=10, max_duration_seconds=10,
                        cookies_file=None, progress=lambda _m: None)
    assert list(tmp_path.glob("source.*")) == []


def test_upload_size_mismatch_is_a_structured_failure(tmp_path):
    class Shrinking(_FakeS3):
        def head_object(self, *, Bucket, Key):  # noqa: N803, ARG002
            return {"ContentLength": 1}

    source = _source(tmp_path)
    s3 = Shrinking()
    with pytest.raises(DownloadError, match="UPLOAD_FAILED"):
        upload_source(
            s3, bucket="b", source=source,
            receipt={"track_id": "t", "object_key": "k", "sha256": source.sha256},
            progress=lambda _m: None,
        )
    # No receipt for an object that failed its size check (A12: receipt LAST).
    assert "sources/t/source.json" not in s3.objects
    assert [o for o in s3.order if "source.json" in o] == []

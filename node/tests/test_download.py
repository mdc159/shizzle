"""Download executor pieces that need no network: probe validation, error
classification, the S3 upload order (source.mp4, verify, source.json LAST)."""

from __future__ import annotations

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

    def put_object(self, *, Bucket, Key, Body, ContentType, IfNoneMatch=None):  # noqa: N803, ARG002
        if IfNoneMatch == "*" and Key in self.objects:
            raise _Precondition()
        data = Body.read() if hasattr(Body, "read") else Body
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
    """First writer wins: a 412 on source.mp4 adopts the owner's receipt once it
    exists and is a retryable race while it does not."""
    s3 = _FakeS3()
    source = _source(tmp_path)
    s3.objects["sources/t/source.mp4"] = b"the other node's bytes"
    receipt = {"track_id": "t", "object_key": "sources/t/source.mp4", "sha256": source.sha256}
    with pytest.raises(DownloadError, match="SOURCE_RACE"):
        upload_source(s3, bucket="b", source=source, receipt=receipt, progress=lambda _m: None)
    assert s3.objects["sources/t/source.mp4"] == b"the other node's bytes"
    s3.objects["sources/t/source.json"] = json.dumps({"sha256": "a" * 64, "bytes": 22}).encode()
    adopted = upload_source(s3, bucket="b", source=source, receipt=receipt, progress=lambda _m: None)
    assert adopted == {"sha256": "a" * 64, "bytes": 22}
    assert s3.order == []  # nothing written by the stale node


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
    with pytest.raises(DownloadError, match="UPLOAD_FAILED"):
        upload_source(
            Shrinking(), bucket="b", source=source,
            receipt={"track_id": "t", "object_key": "k", "sha256": source.sha256},
            progress=lambda _m: None,
        )

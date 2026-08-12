"""Unit tests for ``lossless_intake.download_package`` (moto-backed S3).

The package fetch is the WS5 half that runs with no RunPod and no ffmpeg: pure
S3 + the interface contract (handoff.json present means the worker is done).
These three tests cover the contract gate, the happy path, and idempotency.
"""

from __future__ import annotations

import json
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shizzle_server.publish.lossless_intake import IntakeError, download_package

BUCKET = "shizzle-intake-test"
PREFIX = "tracks/abc/1/separation"
ROLES = ("vocals", "drums", "bass", "guitar", "piano", "shizzle")


def _make_handoff(stem_sizes: dict[str, int]) -> dict:
    return {
        "interface": "lossless-stem-v1",
        "separation": {"sample_count": 44100, "sample_rate_hz": 44100},
        "stems": [
            {"role": r, "file": f"stems/{r}.wav", "bytes": stem_sizes[r], "sha256": "x"}
            for r in ROLES
        ],
    }


def _seed_package(s3, stem_sizes: dict[str, int], *, with_handoff: bool = True) -> None:
    for role, size in stem_sizes.items():
        s3.put_object(
            Bucket=BUCKET, Key=f"{PREFIX}/stems/{role}.wav", Body=b"x" * size
        )
    if with_handoff:
        body = json.dumps(_make_handoff(stem_sizes)).encode()
        s3.put_object(Bucket=BUCKET, Key=f"{PREFIX}/handoff.json", Body=body)


@pytest.fixture
def fake_aws_credentials(monkeypatch):
    """Keep this machine's real AWS creds + R2 endpoint override away from moto."""
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


def test_download_package_missing_handoff_raises(s3, tmp_path: Path):
    """Stems present but no handoff.json -> the package has not crossed the interface."""
    _seed_package(s3, dict.fromkeys(ROLES, 10), with_handoff=False)
    with pytest.raises(IntakeError, match="has not crossed the interface"):
        download_package(s3, BUCKET, PREFIX, tmp_path / "pkg")


def test_download_package_happy_path_downloads_handoff_and_six_stems(s3, tmp_path: Path):
    sizes = {r: 10 + i for i, r in enumerate(ROLES)}
    _seed_package(s3, sizes)

    out = download_package(s3, BUCKET, PREFIX, tmp_path / "pkg")

    assert (out / "handoff.json").exists()
    for role in ROLES:
        stem = out / "stems" / f"{role}.wav"
        assert stem.exists()
        assert stem.stat().st_size == sizes[role]


def test_download_package_idempotent_skips_existing_matching_files(s3, tmp_path: Path):
    sizes = dict.fromkeys(ROLES, 10)
    _seed_package(s3, sizes)
    dest = tmp_path / "pkg"

    class _Rec:
        def __init__(self, inner):
            self._inner = inner
            self.downloads = 0

        def __getattr__(self, name):
            attr = getattr(self._inner, name)
            if not callable(attr):
                return attr

            def wrapped(*args, **kwargs):
                if name == "download_file":
                    self.downloads += 1
                return attr(*args, **kwargs)

            return wrapped

    rec = _Rec(s3)
    download_package(rec, BUCKET, PREFIX, dest)
    first = rec.downloads
    assert first == 7  # handoff + six stems

    # Second call, nothing changed on the remote: every file already matches
    # its declared size, so download_file must not fire again.
    download_package(rec, BUCKET, PREFIX, dest)
    assert rec.downloads == first
    for role in ROLES:
        assert (dest / "stems" / f"{role}.wav").stat().st_size == sizes[role]

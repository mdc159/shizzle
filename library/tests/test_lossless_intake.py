"""Unit tests for ``lossless_intake.download_package`` (moto-backed S3).

The package fetch is the WS5 half that runs with no RunPod and no ffmpeg: pure
S3 + the interface contract (handoff.json present means the worker is done).
These three tests cover the contract gate, the happy path, and idempotency.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from shizzle_server.publish import lossless_intake
from shizzle_server.publish.lossless_intake import (
    IntakeError,
    Package,
    common_gain_db,
    derive_video,
    download_package,
)

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


def test_download_package_rejects_path_traversal(s3, tmp_path: Path):
    handoff = _make_handoff(dict.fromkeys(ROLES, 10))
    handoff["stems"][0]["file"] = "../../evil"
    s3.put_object(
        Bucket=BUCKET,
        Key=f"{PREFIX}/handoff.json",
        Body=json.dumps(handoff).encode(),
    )

    with pytest.raises(IntakeError, match="escapes package directory"):
        download_package(s3, BUCKET, PREFIX, tmp_path / "pkg")
    assert not (tmp_path / "evil").exists()


@pytest.mark.parametrize(
    ("true_peak", "gain"),
    [(-2.0, 0.0), (-1.0, 0.0), (-0.96, -0.1), (-0.9, -0.1), (0.0, -1.0)],
)
def test_common_gain_never_rounds_toward_zero(true_peak: float, gain: float):
    assert common_gain_db(true_peak) == gain


def test_transform_emits_player_timeline_contract(tmp_path: Path, monkeypatch):
    pkg = Package(
        root=tmp_path / "pkg",
        handoff={
            "source": {"sha256": "abc", "object_key": "sources/id/source.mp4"},
            "separation": {
                "sample_count": 103_414,
                "worker_image": "worker:sha",
            },
        },
    )
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")

    monkeypatch.setattr(lossless_intake, "measure_default_mix_true_peak", lambda _pkg: -2.0)

    def fake_encode(_wav: Path, out: Path) -> None:
        out.write_bytes(b"audio")

    def fake_video(_source: Path, out: Path, _duration: float, maxrate_kbps=1200) -> None:
        out.write_bytes(b"video")

    monkeypatch.setattr(lossless_intake, "encode_stem", fake_encode)
    monkeypatch.setattr(lossless_intake, "derive_video", fake_video)

    manifest = lossless_intake.transform(pkg, source, tmp_path / "candidate", "T", "A")
    assert manifest["timeline"] == {
        "start_ms": 0,
        "duration_ms": int(pkg.duration_seconds * 1000),
        "sample_rate_hz": 44100,
    }


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg required"
)
def test_derive_video_caps_and_probes_synthetic_fixture(tmp_path: Path):
    source = tmp_path / "synthetic.mp4"
    subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error", "-f", "lavfi",
            "-i", "color=c=black:s=160x90:r=30:d=2", "-pix_fmt", "yuv420p",
            str(source),
        ],
        check=True,
    )
    out = tmp_path / "video.mp4"
    derive_video(source, out, 0.75)
    actual = float(subprocess.run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip())
    assert abs(actual - 0.75) <= 0.5

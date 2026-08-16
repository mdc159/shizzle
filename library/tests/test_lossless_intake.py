"""Unit tests for ``lossless_intake.download_package`` (moto-backed S3).

The package fetch is the WS5 half that runs with no RunPod and no ffmpeg: pure
S3 + the interface contract (handoff.json present means the worker is done).
These three tests cover the contract gate, the happy path, and idempotency.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from shizzle_server.publish import lossless_intake
from shizzle_server.publish.lossless_intake import (
    IntakeError,
    Package,
    _package_path,
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
    monkeypatch.setattr(lossless_intake, "audit_candidate", _passing_candidate_audits)

    manifest = lossless_intake.transform(pkg, source, tmp_path / "candidate", "T", "A")
    assert manifest["timeline"] == {
        "start_ms": 0,
        "duration_ms": int(round(pkg.duration_seconds * 1000)),
        "sample_rate_hz": 44100,
    }
    assert manifest["delivery_profile"] == "shizzle-browser-v1"
    assert len(manifest["integrity"]["objects"]) == 7


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
    assert abs(actual - 0.75) <= 0.05


def _write_verification_package(root: Path, *, sample_count: int = 10) -> dict:
    stems = []
    for role in ROLES:
        path = root / "stems" / f"{role}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(role.encode())
        stems.append({
            "role": role,
            "file": f"stems/{role}.wav",
            "bytes": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    handoff = {
        "interface": "lossless-stem-v1",
        "source": {"object_key": "sources/id/source.mp4", "sha256": "a" * 64},
        "separation": {
            "model": "htdemucs_6s",
            "model_version": "test",
            "worker_image": "worker:test",
            "sample_rate_hz": 44100,
            "channels": 2,
            "sample_format": "f32le",
            "start_sample": 0,
            "sample_count": sample_count,
        },
        "stems": stems,
    }
    (root / "handoff.json").write_text(json.dumps(handoff))
    return handoff


def test_verification_path_containment_rejects_escape(tmp_path: Path):
    root = tmp_path / "package"
    root.mkdir()
    with pytest.raises(IntakeError, match="escapes package directory"):
        _package_path(root, "../../outside.wav")


def test_load_rejects_duplicate_roles_before_deduplication(tmp_path: Path):
    root = tmp_path / "package"
    root.mkdir()
    handoff = _write_verification_package(root)
    handoff["stems"][1]["role"] = "vocals"
    (root / "handoff.json").write_text(json.dumps(handoff))

    with pytest.raises(IntakeError, match="stem must map"):
        lossless_intake.load_and_verify_package(root)


def test_load_rejects_actual_stem_sample_count_mismatch(tmp_path: Path, monkeypatch):
    root = tmp_path / "package"
    root.mkdir()
    _write_verification_package(root, sample_count=10)
    monkeypatch.setattr(
        lossless_intake,
        "_run",
        lambda _cmd: SimpleNamespace(stdout=json.dumps({"streams": [{
            "codec_name": "pcm_f32le",
            "sample_rate": "44100",
            "channels": 2,
            "duration_ts": 9,
        }]})),
    )

    with pytest.raises(IntakeError, match="sample count 9 != handoff 10"):
        lossless_intake.load_and_verify_package(root)


def test_silent_mix_integrity_is_standard_json(tmp_path: Path, monkeypatch):
    pkg = Package(
        root=tmp_path / "pkg",
        handoff={
            "source": {"sha256": "abc", "object_key": "sources/id/source.mp4"},
            "separation": {"sample_count": 44100, "worker_image": "worker:sha"},
        },
    )
    monkeypatch.setattr(
        lossless_intake, "measure_default_mix_true_peak", lambda _pkg: float("-inf")
    )
    monkeypatch.setattr(
        lossless_intake, "encode_stem", lambda _wav, out: out.write_bytes(b"audio")
    )
    monkeypatch.setattr(
        lossless_intake,
        "derive_video",
        lambda _source, out, _duration, **_kwargs: out.write_bytes(b"video"),
    )
    monkeypatch.setattr(lossless_intake, "audit_candidate", _passing_candidate_audits)

    manifest = lossless_intake.transform(
        pkg, tmp_path / "source.mp4", tmp_path / "candidate", "T", "A"
    )
    assert manifest["integrity"]["default_mix_true_peak_dbtp"] is None
    json.dumps(manifest, allow_nan=False)


def _passing_candidate_audits(_candidate: Path, _duration: float) -> list[dict]:
    artifacts = [f"stems/{role}.m4a" for role in ROLES] + ["video.mp4"]
    return [
        {
            "artifact": artifact,
            "bytes": 5,
            "sha256": hashlib.sha256(artifact.encode()).hexdigest(),
            "passed": True,
            "full_decode": "pass",
            "issues": [],
        }
        for artifact in artifacts
    ]


def test_candidate_audit_fails_closed_on_release_blocking_issue(
    tmp_path: Path, monkeypatch
):
    def failed_audio(*_args, artifact: str, **_kwargs):
        return {
            "artifact": artifact,
            "bytes": 1,
            "sha256": "a" * 64,
            "passed": False,
            "full_decode": "fail",
            "issues": [{"code": "audio-full-decode"}],
        }

    monkeypatch.setattr(lossless_intake, "audit_audio_file", failed_audio)
    monkeypatch.setattr(
        lossless_intake,
        "audit_video_file",
        lambda *_args, artifact, **_kwargs: {
            "artifact": artifact,
            "bytes": 1,
            "sha256": "b" * 64,
            "passed": True,
            "full_decode": "pass",
            "issues": [],
        },
    )

    with pytest.raises(IntakeError, match="audio-full-decode"):
        lossless_intake.audit_candidate(tmp_path, 1.0)


def test_stage_ignores_stale_manifest_when_collecting_media(tmp_path: Path):
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "video.mp4").write_bytes(b"video")
    (candidate / "manifest.json").write_text("stale")

    class S3:
        def upload_file(self, *_args, **_kwargs):
            pass

    staged = lossless_intake.stage(
        S3(), BUCKET, uuid.uuid4(), 1, candidate, {"version": 3}
    )
    assert [item.file for item in staged] == ["video.mp4", "manifest.json"]


def test_track_duration_tolerance_covers_h264_frame_quantization():
    """wave3 #5: at 30 fps one frame is ~33 ms, and H.264 frame quantization can
    drift a clean encode by ~1 frame in either direction. The video-duration
    gate needs at least one frame-duration of headroom so a clean single-frame-
    early end is not rejected."""
    from shizzle_server.publish.delivery_profile import TRACK_DURATION_TOLERANCE_SEC

    assert TRACK_DURATION_TOLERANCE_SEC >= 0.100


def test_derive_video_tolerates_clean_frame_quantization(
    monkeypatch, tmp_path: Path
):
    """wave3 #5: a clean encode ending one 30 fps frame (~33 ms) short of the
    stem timeline must pass the gate; only a deviation beyond the widened
    tolerance raises IntakeError."""
    source = tmp_path / "src.mp4"
    source.write_bytes(b"")
    out = tmp_path / "video.mp4"

    def _fake_run_factory(probe_duration: str):
        def _fake_run(cmd, timeout=1800):  # noqa: ARG005
            if cmd[0] == "ffmpeg":
                out.write_bytes(b"mp4")
                return subprocess.CompletedProcess(cmd, 0, "", "")
            return subprocess.CompletedProcess(cmd, 0, probe_duration, "")

        return _fake_run

    # 33 ms short of a 1.0s timeline: one frame at 30 fps — must pass.
    monkeypatch.setattr(lossless_intake, "_run", _fake_run_factory("0.967"))
    derive_video(source, out, 1.0)
    assert out.exists()

    # 150 ms short: beyond the widened tolerance — must raise.
    monkeypatch.setattr(lossless_intake, "_run", _fake_run_factory("0.850"))
    out.unlink(missing_ok=True)
    with pytest.raises(IntakeError):
        derive_video(source, out, 1.0)

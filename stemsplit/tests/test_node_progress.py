"""The handler runs for a worker node with a progress callback and explicit device.

Companion to test_wave3_hardening: same fake S3 and separation stub, but no
``runpod`` module anywhere — the node path must not import it.
"""

import json
import sys
from pathlib import Path

import pytest

import lossless_handler
from lossless_handler import handler
from lossless_worker import ROLES


@pytest.fixture
def no_runpod(monkeypatch):
    monkeypatch.delitem(sys.modules, "runpod", raising=False)
    monkeypatch.setitem(sys.modules, "runpod", None)  # import would fail loudly


def _stub_run(monkeypatch, seen: dict) -> None:
    def fake_run(_source, out_dir, *, track_id, generation, source_key, worker_image,
                 device=None, heartbeat=None):
        seen["device"] = device
        stems_dir = Path(out_dir) / "stems"
        stems_dir.mkdir(parents=True, exist_ok=True)
        stems = []
        for role in ROLES:
            path = stems_dir / f"{role}.wav"
            path.write_bytes(b"RIFF" + role.encode())
            stems.append({"role": role, "file": f"stems/{role}.wav", "bytes": path.stat().st_size,
                          "sha256": "0" * 64})
        heartbeat("separate: 100%")
        return {
            "interface": "lossless-stem-v1", "track_id": track_id, "generation": generation,
            "source": {"object_key": source_key, "sha256": "1" * 64},
            "separation": {"model": "htdemucs_6s", "model_version": "t", "worker_image": worker_image,
                           "sample_rate_hz": 44100, "channels": 2, "sample_format": "f32le",
                           "start_sample": 0, "sample_count": 10},
            "stems": stems, "_timings": {},
        }

    monkeypatch.setattr(lossless_handler, "run", fake_run)


@pytest.mark.usefixtures("no_runpod")
def test_handler_reports_through_the_callback_and_passes_the_device(monkeypatch):
    from test_wave3_hardening import _FakeS3

    s3 = _FakeS3()
    s3.store("sources/t/source.mp4", b"x" * 200_000)
    monkeypatch.setattr(lossless_handler, "create_s3_client", lambda: s3)
    def fake_download(_s3, _bucket, _key, path, _heartbeat):
        path.write_bytes(b"src")

    def fake_upload(_s3, _bucket, key, path, _heartbeat):
        s3.store(key, path.read_bytes())
        return {"key": key, "etag": s3.etags[key]}

    monkeypatch.setattr(lossless_handler, "download_source", fake_download)
    monkeypatch.setattr(lossless_handler, "upload_file", fake_upload)
    seen: dict = {}
    _stub_run(monkeypatch, seen)
    phases: list[str] = []

    result = handler(
        {
            "id": "node:11111111-1111-4111-8111-111111111111",
            "input": {
                "track_id": "t", "generation": 1, "idempotency_key": "job:0",
                "bucket": "b", "input_key": "sources/t/source.mp4",
                "output_prefix": "tracks/t/1/separation/", "device": "mps",
            },
        },
        progress=phases.append,
    )

    assert result["status"] == "COMPLETED"
    assert seen["device"] == "mps"
    assert phases and phases[0].startswith("dispatch: recording node:")
    assert any(p.startswith("handoff: writing") for p in phases)
    receipt = json.loads(s3.objects[f"{result['package_prefix']}/dispatch.json"])
    assert receipt["runpod_job_id"] == "node:11111111-1111-4111-8111-111111111111"
    assert f"{result['package_prefix']}/handoff.json" in s3.objects

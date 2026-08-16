"""wave3 hardening regression tests for the lossless worker.

#2: handler() isolates each dispatch under an immutable attempt prefix, so a
    retry cannot replace another receipt or mutate a completed package.
#4: the S3 transfer progress callback closure is mutated from multiple
    transfer-manager threads during multipart transfers; the lock keeps the
    reported percentage monotonic and bounded.
"""

import hashlib
import json
import sys
import threading
from pathlib import Path
from types import ModuleType

import s3_ops
from lossless_handler import _attempt_prefix, handler
from lossless_worker import ROLES


class _FakeServerless:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def progress_update(self, _job, msg) -> None:  # noqa: ANN001
        self.messages.append(msg)


class _FakeS3:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.puts: list[dict] = []

    def delete_object(self, *, _Bucket, Key) -> None:  # noqa: ANN001, N803
        self.deleted.append(Key)

    def put_object(self, **kwargs) -> None:  # noqa: ANN003
        self.puts.append(kwargs)


def test_attempt_prefix_is_deterministic_and_isolates_retries():
    base = "tracks/T1/1/separation"
    first = _attempt_prefix(base, "job-1:0")
    assert first == _attempt_prefix(f"{base}/", "job-1:0")
    assert first != _attempt_prefix(base, "job-1:1")
    assert first.startswith(f"{base}/attempts/")


def test_handler_isolates_each_dispatch_attempt(monkeypatch):
    """A retry writes a distinct receipt/package and cannot mutate its peer."""
    serverless = _FakeServerless()
    runpod = ModuleType("runpod")
    runpod.serverless = serverless  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "runpod", runpod)

    fake_s3 = _FakeS3()
    monkeypatch.setattr("lossless_handler.create_s3_client", lambda: fake_s3)

    def _download(_s3, _bucket, _key, path, _heartbeat=None):  # noqa: ANN001
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"src")

    monkeypatch.setattr("lossless_handler.download_source", _download)

    def _run(_source, _out, **_kwargs):  # noqa: ANN001
        return {
            "interface": "lossless-stem-v1",
            "separation": {"sample_count": 10},
            "_timings": {"ok": 1},
        }

    monkeypatch.setattr("lossless_handler.run", _run)

    uploaded: list[str] = []

    def _upload(_s3, _bucket, key, path, _heartbeat=None):  # noqa: ANN001
        uploaded.append(key)
        return {"file": Path(path).name, "key": key, "sha256": "x", "size_bytes": 1}

    monkeypatch.setattr("lossless_handler.upload_file", _upload)

    prefix = "tracks/T1/1/separation"
    dispatch_key = "job-1:0"
    result = handler({
        "id": "rp-accepted",
        "input": {
            "track_id": "T1",
            "generation": 1,
            "idempotency_key": dispatch_key,
            "bucket": "bkt",
            "input_key": "sources/T1/source.mp4",
            "output_prefix": prefix,
        }
    })

    attempt_id = hashlib.sha256(dispatch_key.encode()).hexdigest()
    attempt_prefix = f"{prefix}/attempts/{attempt_id}"
    handoff_key = f"{attempt_prefix}/handoff.json"
    # The shared marker is never deleted — a prior valid package survives a race.
    assert handoff_key not in fake_s3.deleted
    assert fake_s3.deleted == []
    # handoff.json is the atomic promotion: written LAST, after every stem.
    assert uploaded[-1] == handoff_key
    assert [k for k in uploaded if k.endswith(".wav")] == [
        f"{attempt_prefix}/stems/{role}.wav" for role in ROLES
    ]
    assert len(fake_s3.puts) == 1
    receipt = fake_s3.puts[0]
    assert receipt["Key"] == f"{attempt_prefix}/dispatch.json"
    assert receipt["ContentType"] == "application/json"
    assert json.loads(receipt["Body"]) == {
        "runpod_job_id": "rp-accepted",
        "idempotency_key": "job-1:0",
        "track_id": "T1",
        "generation": 1,
        "package_prefix": attempt_prefix,
    }
    assert result["status"] == "COMPLETED"
    assert result["package_prefix"] == attempt_prefix


def test_transfer_callback_is_thread_safe_and_monotonic():
    """wave3 #4: under concurrent multipart-callback invocation the closure
    state stays consistent — heartbeats are well-formed, non-decreasing, and
    reach 100% when the full byte total is accounted for."""
    total = 100_000
    messages: list[str] = []
    cb = s3_ops._transfer_callback(messages.append, "upload: x.wav", total)

    n_threads = 20
    per_thread = total // n_threads  # 5000
    chunk = per_thread // 50  # 100 -> 50 callbacks/thread
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            remaining = per_thread
            while remaining > 0:
                cb(chunk)
                remaining -= chunk
        except BaseException as exc:  # pragma: no cover - fail loud
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    pcts = []
    for m in messages:
        assert m.startswith("upload: x.wav: ")
        pcts.append(int(m.rsplit(": ", 1)[-1].rstrip("%")))
    # Lock guarantee: percentages only ever advance.
    assert pcts == sorted(pcts)
    assert pcts[-1] == 100
    # Every emitted percentage is a 5% bucket in the documented range.
    assert all(0 <= p <= 100 and p % 5 == 0 for p in pcts)

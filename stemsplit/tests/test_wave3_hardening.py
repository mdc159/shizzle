"""wave3 hardening regression tests for the lossless worker.

#2: handler() isolates each dispatch under an immutable attempt prefix, so a
    retry cannot replace another receipt or mutate a completed package.
#4: the S3 transfer progress callback closure is mutated from multiple
    transfer-manager threads during multipart transfers; the lock keeps the
    reported percentage monotonic and bounded.
#32: a redelivery of a completed dispatch writes nothing, and concurrent
    replays of one dispatch converge on exactly one owner (SUPERSEDED loser).
"""

import hashlib
import json
import sys
import threading
from pathlib import Path
from types import ModuleType

from botocore.exceptions import ClientError

import s3_ops
from lossless_handler import _attempt_prefix, handler
from lossless_worker import ROLES


class _FakeServerless:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def progress_update(self, _job, msg) -> None:  # noqa: ANN001
        self.messages.append(msg)


def _client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "FakeOp",
    )


class _FakeS3:
    """In-memory store recording every mutating call and honouring
    IfNoneMatch="*" conditional puts with a 412 PreconditionFailed."""

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.puts: list[dict] = []
        self.objects: dict[str, bytes] = {}
        self.mutations: list[tuple[str, str]] = []
        self.heads: list[str] = []
        # Optional barrier released inside put_object before the conditional
        # check, so tests can race two claims past the completion check.
        self.claim_barrier: threading.Barrier | None = None

    def head_object(self, *, Bucket, Key) -> dict:  # noqa: ANN001, ARG002, N803
        # Bucket is accepted (boto3 kwarg) but unused; keep the real name.
        self.heads.append(Key)
        if Key not in self.objects:
            raise _client_error("404", 404)
        return {"ContentLength": len(self.objects[Key])}

    def put_object(self, **kwargs) -> None:  # noqa: ANN003
        if self.claim_barrier is not None:
            self.claim_barrier.wait(timeout=10)
        key = kwargs["Key"]
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise _client_error("PreconditionFailed", 412)
        self.puts.append(kwargs)
        self.mutations.append(("put_object", key))
        body = kwargs.get("Body", b"")
        self.objects[key] = body if isinstance(body, bytes) else body.read()

    def upload_file(self, path, _bucket, key, _Callback=None, **_kw) -> None:  # noqa: ANN001, ANN003
        self.mutations.append(("upload_file", key))
        self.objects[key] = Path(path).read_bytes()

    def upload_fileobj(self, fileobj, _bucket, key, **_kw) -> None:  # noqa: ANN001, ANN003
        self.mutations.append(("upload_fileobj", key))
        self.objects[key] = fileobj.read()

    def copy(self, **kwargs) -> None:  # noqa: ANN003
        self.mutations.append(("copy", str(kwargs.get("Key", "?"))))

    def delete_object(self, *, _Bucket, Key) -> None:  # noqa: ANN001, N803
        self.deleted.append(Key)
        self.mutations.append(("delete_object", Key))


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


def _dispatch_job(job_id: str = "rp-accepted", dispatch_key: str = "job-1:0") -> dict:
    return {
        "id": job_id,
        "input": {
            "track_id": "T1",
            "generation": 1,
            "idempotency_key": dispatch_key,
            "bucket": "bkt",
            "input_key": "sources/T1/source.mp4",
            "output_prefix": "tracks/T1/1/separation",
        },
    }


def _patch_handler_env(monkeypatch, fake_s3: _FakeS3) -> list[Path]:
    """Wire handler()'s collaborators onto the fake store; return the run spy."""
    runpod = ModuleType("runpod")
    runpod.serverless = _FakeServerless()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "runpod", runpod)
    monkeypatch.setattr("lossless_handler.create_s3_client", lambda: fake_s3)

    def _download(_s3, _bucket, _key, path, _heartbeat=None):  # noqa: ANN001
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"src")

    monkeypatch.setattr("lossless_handler.download_source", _download)

    runs: list[Path] = []

    def _run(_source, out, **_kwargs):  # noqa: ANN001
        runs.append(out)
        stems = out / "stems"
        stems.mkdir(parents=True, exist_ok=True)
        for role in ROLES:
            (stems / f"{role}.wav").write_bytes(f"wav-{role}".encode())
        return {
            "interface": "lossless-stem-v1",
            "separation": {"sample_count": 10},
            "_timings": {"ok": 1},
        }

    monkeypatch.setattr("lossless_handler.run", _run)

    def _upload(s3, bucket, key, path, _heartbeat=None):  # noqa: ANN001
        s3.upload_file(str(path), bucket, key)
        return {
            "file": Path(path).name,
            "key": key,
            "sha256": "x",
            "size_bytes": Path(path).stat().st_size,
        }

    monkeypatch.setattr("lossless_handler.upload_file", _upload)
    return runs


def test_handler_replay_of_completed_attempt_writes_nothing(monkeypatch):
    """#32: a redelivery of a completed dispatch (same job id + idempotency
    key) is an idempotent no-op: no mutating S3 call of any kind, and every
    stored object stays byte-identical."""
    fake_s3 = _FakeS3()
    runs = _patch_handler_env(monkeypatch, fake_s3)
    job = _dispatch_job()

    first = handler(job)
    assert first["status"] == "COMPLETED"
    assert len(runs) == 1
    stored_bytes = dict(fake_s3.objects)
    mutation_count = len(fake_s3.mutations)
    heads_after_first = len(fake_s3.heads)
    # A complete package: receipt + six stems + handoff (written last).
    prefix = first["package_prefix"]
    assert fake_s3.mutations[-1] == ("upload_file", f"{prefix}/handoff.json")

    replay = handler(job)

    assert replay["status"] == "COMPLETED"
    assert replay["package_prefix"] == first["package_prefix"]
    assert replay.keys() == first.keys()
    # No second separation, no second download: the guard fires first.
    assert len(runs) == 1
    # Zero additional mutating calls (put_object, upload_file, upload_fileobj,
    # copy, delete_object are all recorded).
    assert len(fake_s3.mutations) == mutation_count
    # Every stored object is byte-identical to the completed attempt.
    assert fake_s3.objects == stored_bytes
    # The replay probed completion with exactly one HEAD of the handoff key
    # and never read or parsed the stored document.
    assert fake_s3.heads[heads_after_first:] == [f"{prefix}/handoff.json"]


def test_concurrent_replays_only_one_claims(monkeypatch):
    """#32: two workers racing one dispatch both pass the completion check,
    then contend the conditional receipt PUT; exactly one uploads the package
    (handoff last) and the other returns SUPERSEDED having written nothing."""
    fake_s3 = _FakeS3()
    # Both threads reach the claim having already seen no handoff.
    fake_s3.claim_barrier = threading.Barrier(2)
    _patch_handler_env(monkeypatch, fake_s3)

    results: list[dict] = []
    errors: list[BaseException] = []

    def _work() -> None:
        try:
            results.append(handler(_dispatch_job()))
        except BaseException as exc:  # pragma: no cover - fail loud
            errors.append(exc)

    threads = [threading.Thread(target=_work) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors
    assert sorted(r["status"] for r in results) == ["COMPLETED", "SUPERSEDED"]
    loser = next(r for r in results if r["status"] == "SUPERSEDED")
    assert loser["uploads"] == 0

    prefix = loser["package_prefix"]
    # The complete mutation log is exactly ONE package: the winner's claim,
    # six stem uploads, handoff last. The loser contributed nothing.
    assert fake_s3.mutations == [
        ("put_object", f"{prefix}/dispatch.json"),
        *(("upload_file", f"{prefix}/stems/{role}.wav") for role in ROLES),
        ("upload_file", f"{prefix}/handoff.json"),
    ]
    assert set(fake_s3.objects) == {
        f"{prefix}/dispatch.json",
        f"{prefix}/handoff.json",
        *(f"{prefix}/stems/{role}.wav" for role in ROLES),
    }


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

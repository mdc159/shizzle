"""wave3 hardening regression tests for the lossless worker.

#2: handler() isolates each dispatch under an immutable attempt prefix, so a
    retry cannot replace another receipt or mutate a completed package.
#4: the S3 transfer progress callback closure is mutated from multiple
    transfer-manager threads during multipart transfers; the lock keeps the
    reported percentage monotonic and bounded.
#32: a redelivery of a completed dispatch writes nothing and returns the
    stored completion metadata; a non-owner raises AttemptClaimedError so the
    RunPod job fails (recovery via B12); an abandoned claim is reclaimed
    after the stale window; a late predecessor cannot publish a handoff.
"""

import hashlib
import io
import json
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest
from botocore.exceptions import ClientError

import lossless_handler
import s3_ops
from lossless_handler import AttemptClaimedError, _attempt_prefix, handler
from lossless_worker import ROLES

T0 = datetime(2026, 1, 1, tzinfo=UTC)


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
    """In-memory store recording every mutating call, honouring IfNoneMatch /
    IfMatch conditional writes with a 412 PreconditionFailed, and serializing
    the membership check and the write under a lock (S3 atomicity, not the GIL).
    """

    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.puts: list[dict] = []
        self.objects: dict[str, bytes] = {}
        self.etags: dict[str, str] = {}
        self.mutations: list[tuple[str, str]] = []
        self._lock = threading.Lock()
        # Optional barrier released before the receipt claim is evaluated, so
        # tests can race two claims past the completion check.
        self.claim_barrier: threading.Barrier | None = None
        # Optional hook run inside the lock before an IfMatch PUT is evaluated
        # (e.g. a usurper rewriting the receipt under a stale reclaim).
        self.on_ifmatch = None

    def get_object(self, *, Bucket, Key) -> dict:  # noqa: ANN001, ARG002, N803
        # Bucket is accepted (boto3 kwarg) but unused; keep the real name.
        if Key not in self.objects:
            raise _client_error("NoSuchKey", 404)
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": self.etags[Key]}

    def store(self, key: str, body: bytes) -> None:
        """Direct write (test hook) that refreshes the ETag like a real PUT."""
        with self._lock:
            self.objects[key] = body
            self.etags[key] = f'"{hashlib.sha1(body).hexdigest()}"'

    def put_object(self, **kwargs) -> None:  # noqa: ANN003
        if self.claim_barrier is not None and kwargs["Key"].endswith("/dispatch.json"):
            self.claim_barrier.wait(timeout=10)
        body = kwargs.get("Body", b"")
        body = body if isinstance(body, bytes) else body.read()
        with self._lock:  # membership check + write are one critical section
            key = kwargs["Key"]
            if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
                raise _client_error("PreconditionFailed", 412)
            if kwargs.get("IfMatch") is not None:
                if self.on_ifmatch is not None:
                    self.on_ifmatch()  # runs under the lock (test hook)
                if self.etags.get(key) != kwargs["IfMatch"]:
                    raise _client_error("PreconditionFailed", 412)
            self.puts.append(kwargs)
            self.mutations.append(("put_object", key))
            self.objects[key] = body
            self.etags[key] = f'"{hashlib.sha1(body).hexdigest()}"'

    def upload_file(self, path, _bucket, key, _Callback=None, **_kw) -> None:  # noqa: ANN001, ANN003
        body = Path(path).read_bytes()
        with self._lock:
            self.mutations.append(("upload_file", key))
            self.objects[key] = body
            self.etags[key] = f'"{hashlib.sha1(body).hexdigest()}"'

    def upload_fileobj(self, fileobj, _bucket, key, **_kw) -> None:  # noqa: ANN001, ANN003
        with self._lock:
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
    assert uploaded == [f"{attempt_prefix}/stems/{role}.wav" for role in ROLES]
    assert fake_s3.mutations[-1] == ("put_object", handoff_key)
    assert len(fake_s3.puts) == 2
    receipt = fake_s3.puts[0]
    assert receipt["Key"] == f"{attempt_prefix}/dispatch.json"
    assert receipt["ContentType"] == "application/json"
    assert receipt["IfNoneMatch"] == "*"
    receipt_doc = json.loads(receipt["Body"])
    assert receipt_doc["runpod_job_id"] == "rp-accepted"
    assert receipt_doc["idempotency_key"] == "job-1:0"
    assert receipt_doc["track_id"] == "T1"
    assert receipt_doc["generation"] == 1
    assert receipt_doc["package_prefix"] == attempt_prefix
    assert receipt_doc["claimed_at"]
    assert receipt_doc["execution_id"]
    handoff_put = fake_s3.puts[1]
    assert handoff_put["Key"] == handoff_key
    assert handoff_put["IfNoneMatch"] == "*"
    assert handoff_put["ContentType"] == "application/json"
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


def _attempt_prefix_for(dispatch_key: str = "job-1:0") -> str:
    return _attempt_prefix("tracks/T1/1/separation", dispatch_key)


def _fake_download(_s3, _bucket, _key, path, _heartbeat=None):  # noqa: ANN001
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"src")


def _fake_upload(s3, bucket, key, path, _heartbeat=None):  # noqa: ANN001
    s3.upload_file(str(path), bucket, key)
    return {
        "file": Path(path).name,
        "key": key,
        "sha256": "x",
        "size_bytes": Path(path).stat().st_size,
    }


def _patch_handler_env(monkeypatch, fake_s3: _FakeS3) -> list[Path]:
    """Wire handler()'s collaborators onto the fake store; return the run spy."""
    runpod = ModuleType("runpod")
    runpod.serverless = _FakeServerless()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "runpod", runpod)
    monkeypatch.setattr("lossless_handler.create_s3_client", lambda: fake_s3)
    monkeypatch.setattr("lossless_handler.download_source", _fake_download)

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
    monkeypatch.setattr("lossless_handler.upload_file", _fake_upload)
    return runs


def _freeze_clock(monkeypatch, start: datetime = T0) -> dict:
    """Inject a controllable clock and a deterministic stale window."""
    clock = {"now": start}
    monkeypatch.setattr(lossless_handler, "_utcnow", lambda: clock["now"])
    monkeypatch.setattr(lossless_handler, "_DISPATCH_STALE_SECONDS", 900.0)
    return clock


def _crash_after_claim(monkeypatch, job: dict, fake_s3: _FakeS3) -> None:
    """First delivery: claims the prefix, then dies before any stem upload."""

    def _crash(_s3, _bucket, _key, _path, _heartbeat=None):  # noqa: ANN001
        raise RuntimeError("worker killed after claim")

    monkeypatch.setattr("lossless_handler.download_source", _crash)
    with pytest.raises(RuntimeError, match="killed"):
        handler(job)
    assert fake_s3.mutations == [("put_object", f"{_attempt_prefix_for()}/dispatch.json")]


def test_handler_replay_of_completed_attempt_writes_nothing(monkeypatch):
    """#32: a redelivery of a completed dispatch (same job id + idempotency
    key) is an idempotent no-op returning the stored completion metadata: no
    mutating S3 call of any kind, every stored object byte-identical."""
    fake_s3 = _FakeS3()
    runs = _patch_handler_env(monkeypatch, fake_s3)
    job = _dispatch_job()

    first = handler(job)
    assert first["status"] == "COMPLETED"
    assert first["sample_count"] == 10
    assert len(runs) == 1
    stored_bytes = dict(fake_s3.objects)
    mutation_count = len(fake_s3.mutations)
    # A complete package: receipt + six stems + handoff (written last).
    prefix = first["package_prefix"]
    assert fake_s3.mutations[-1] == ("put_object", f"{prefix}/handoff.json")

    replay = handler(job)

    assert replay["status"] == "COMPLETED"
    assert replay["package_prefix"] == first["package_prefix"]
    assert replay.keys() == first.keys()
    assert replay["sample_count"] == 10  # from the stored handoff document
    # No second separation, no second download: the guard fires first.
    assert len(runs) == 1
    # Zero additional mutating calls (put_object, upload_file, upload_fileobj,
    # copy, delete_object are all recorded).
    assert len(fake_s3.mutations) == mutation_count
    # Every stored object is byte-identical to the completed attempt.
    assert fake_s3.objects == stored_bytes


def test_concurrent_replays_only_one_claims(monkeypatch):
    """#32: two workers racing one dispatch both pass the completion check,
    then contend the conditional receipt PUT; exactly one uploads the package
    (handoff last) and the other raises AttemptClaimedError, writing nothing."""
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

    assert len(results) == 1 and results[0]["status"] == "COMPLETED"
    assert len(errors) == 1 and isinstance(errors[0], AttemptClaimedError)
    assert "claimed by execution" in str(errors[0])

    prefix = results[0]["package_prefix"]
    # The complete mutation log is exactly ONE package: the winner's claim,
    # six stem uploads, handoff last. The loser contributed nothing.
    assert fake_s3.mutations == [
        ("put_object", f"{prefix}/dispatch.json"),
        *[("upload_file", f"{prefix}/stems/{role}.wav") for role in ROLES],
        ("put_object", f"{prefix}/handoff.json"),
    ]
    assert set(fake_s3.objects) == {
        f"{prefix}/dispatch.json",
        f"{prefix}/handoff.json",
        *(f"{prefix}/stems/{role}.wav" for role in ROLES),
    }


def test_crash_after_claim_is_reclaimed_when_stale(monkeypatch):
    """#32: a claim abandoned past SHIZZLE_DISPATCH_STALE_SECONDS is reclaimed
    by the next redelivery, which rebuilds the full package byte-consistently."""
    fake_s3 = _FakeS3()
    runs = _patch_handler_env(monkeypatch, fake_s3)
    job = _dispatch_job()
    clock = _freeze_clock(monkeypatch)
    prefix = _attempt_prefix_for()
    receipt_key = f"{prefix}/dispatch.json"

    _crash_after_claim(monkeypatch, job, fake_s3)
    stale_etag = fake_s3.etags[receipt_key]
    crash_receipt = json.loads(fake_s3.objects[receipt_key])

    # Redeliver after the stale window: reclaim and complete the package.
    monkeypatch.setattr("lossless_handler.download_source", _fake_download)
    clock["now"] = T0 + timedelta(seconds=901)
    result = handler(job)

    assert result["status"] == "COMPLETED"
    assert result["sample_count"] == 10
    assert len(runs) == 1  # the crashed delivery never reached separation
    # The reclaim was a conditional PUT against the abandoned claim's ETag.
    assert fake_s3.puts[1].get("IfMatch") == stale_etag
    assert "IfNoneMatch" not in fake_s3.puts[1]
    receipt_doc = json.loads(fake_s3.objects[receipt_key])
    assert receipt_doc["claimed_at"] == clock["now"].isoformat()
    assert receipt_doc["execution_id"] != crash_receipt["execution_id"]
    # Full package, handoff last, stems byte-consistent from one execution.
    assert fake_s3.mutations[-1] == ("put_object", f"{prefix}/handoff.json")
    for role in ROLES:
        assert fake_s3.objects[f"{prefix}/stems/{role}.wav"] == f"wav-{role}".encode()
    handoff = json.loads(fake_s3.objects[f"{prefix}/handoff.json"])
    assert handoff["separation"]["sample_count"] == 10
    assert set(fake_s3.objects) == {
        receipt_key,
        f"{prefix}/handoff.json",
        *(f"{prefix}/stems/{role}.wav" for role in ROLES),
    }


def test_crash_after_claim_raises_when_not_stale(monkeypatch):
    """#32: inside the stale window a redelivery raises AttemptClaimedError
    (job fails, orchestrator retries under a fresh key) and writes nothing."""
    fake_s3 = _FakeS3()
    _patch_handler_env(monkeypatch, fake_s3)
    job = _dispatch_job()
    clock = _freeze_clock(monkeypatch)

    _crash_after_claim(monkeypatch, job, fake_s3)
    crash_objects = dict(fake_s3.objects)
    crash_mutations = list(fake_s3.mutations)

    monkeypatch.setattr("lossless_handler.download_source", _fake_download)
    clock["now"] = T0 + timedelta(seconds=60)  # inside the 900 s window

    with pytest.raises(AttemptClaimedError, match="claim is not stale"):
        handler(job)
    assert fake_s3.objects == crash_objects
    assert fake_s3.mutations == crash_mutations


def test_lost_stale_reclaim_reports_owner_and_writes_nothing(monkeypatch):
    """#32: a stale reclaim that loses the IfMatch race raises naming the
    current owner's execution_id and performs no package writes."""
    fake_s3 = _FakeS3()
    runs = _patch_handler_env(monkeypatch, fake_s3)
    job = _dispatch_job()
    clock = _freeze_clock(monkeypatch)
    prefix = _attempt_prefix_for()
    receipt_key = f"{prefix}/dispatch.json"

    _crash_after_claim(monkeypatch, job, fake_s3)

    monkeypatch.setattr("lossless_handler.download_source", _fake_download)
    clock["now"] = T0 + timedelta(seconds=901)  # the crashed claim is stale

    # A usurper rewrites the receipt between our ETag read and our IfMatch PUT.
    def _usurp():
        doc = json.loads(fake_s3.objects[receipt_key])
        doc["execution_id"] = "usurper-execution"
        body = json.dumps(doc).encode()
        fake_s3.objects[receipt_key] = body  # already under the fake's lock
        fake_s3.etags[receipt_key] = f'"{hashlib.sha1(body).hexdigest()}"'

    fake_s3.on_ifmatch = _usurp

    with pytest.raises(AttemptClaimedError, match="usurper-execution"):
        handler(job)

    # The lost reclaim wrote nothing: no separation, no stems, no handoff;
    # the only recorded mutation ever is the original crashed claim.
    assert runs == []
    assert fake_s3.mutations == [("put_object", receipt_key)]
    assert not any(k.endswith((".wav", "handoff.json")) for k in fake_s3.objects)


def test_late_predecessor_cannot_write_handoff(monkeypatch):
    """#32: a predecessor whose claim was reclaimed under it fails the
    ownership re-check and never writes handoff.json."""
    fake_s3 = _FakeS3()
    _patch_handler_env(monkeypatch, fake_s3)
    job = _dispatch_job()
    prefix = _attempt_prefix_for()
    receipt_key = f"{prefix}/dispatch.json"

    def _usurping_upload(s3, bucket, key, path, heartbeat=None):  # noqa: ANN001
        record = _fake_upload(s3, bucket, key, path, heartbeat)
        if key.endswith("shizzle.wav"):  # last stem: the claim is reclaimed under us
            doc = json.loads(s3.objects[receipt_key])
            doc["execution_id"] = "usurper"
            s3.store(receipt_key, json.dumps(doc).encode())
        return record

    monkeypatch.setattr("lossless_handler.upload_file", _usurping_upload)

    with pytest.raises(AttemptClaimedError, match="now owned"):
        handler(job)

    assert f"{prefix}/handoff.json" not in fake_s3.objects
    # All six stems were uploaded before the re-check refused the handoff.
    assert [k for op, k in fake_s3.mutations if op == "upload_file"] == [
        f"{prefix}/stems/{role}.wav" for role in ROLES
    ]


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

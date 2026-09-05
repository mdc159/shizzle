"""Entry points for the lossless-stem worker.

RunPod serverless mode:
    python3 lossless_handler.py            (runpod.serverless.start)

Local mode (the proving path — file in, package out, no S3):
    python3 lossless_handler.py --local <input.mp4> <output_dir> [--track-id ID]

Cloud jobs download the source from S3, run the core pipeline, upload the six
WAVs, and write handoff.json to S3 LAST. Every phase heartbeats through the
RunPod progress channel, so a stall is diagnosable by which phase froze.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from botocore.exceptions import ClientError

from lossless_worker import ROLES, run
from s3_ops import create_s3_client, download_source, upload_file

WORKER_IMAGE = os.getenv("WORKER_IMAGE", "unknown")

# S3 conditional writes (If-None-Match: *) are GA on AWS S3 but unsupported
# by some S3-compatible proving stores. Default on; SHIZZLE_CONDITIONAL_DISPATCH=0
# falls back to an unconditional receipt PUT (logged), leaving the replay guard
# as the completion check alone. Read once at worker start.
_CONDITIONAL_DISPATCH = os.getenv("SHIZZLE_CONDITIONAL_DISPATCH", "1").strip().lower() not in (
    "0",
    "false",
    "no",
    "off",
)

# An attempt claim older than this is abandoned and may be reclaimed by a
# redelivery of the same dispatch. Read once at worker start.
_DISPATCH_STALE_SECONDS = float(os.getenv("SHIZZLE_DISPATCH_STALE_SECONDS", "900"))


class AttemptClaimedError(RuntimeError):
    """This invocation does not own the attempt prefix (someone else claimed,
    reclaimed, or completed it). The RunPod job fails, and the orchestrator
    retries the dispatch under a fresh idempotency key (invariant B12)."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _clean(handoff: dict) -> dict:
    """Strip private keys; the uploaded document is schema-strict."""
    return {k: v for k, v in handoff.items() if not k.startswith("_")}


def _attempt_prefix(base_prefix: str, idempotency_key: str) -> str:
    """Return an immutable, path-safe package prefix for one dispatch attempt."""
    attempt_id = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"{base_prefix.rstrip('/')}/attempts/{attempt_id}"


def _is_not_found(err: ClientError) -> bool:
    """True when a read failed because the object is absent (not a real error)."""
    status = err.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = err.response.get("Error", {}).get("Code", "")
    return status == 404 or code in ("NoSuchKey", "404", "NotFound")


def _is_precondition_failed(err: ClientError) -> bool:
    """True only for a conditional-write loss: code PreconditionFailed, HTTP 412."""
    response = err.response
    return (
        response.get("Error", {}).get("Code") == "PreconditionFailed"
        and response.get("ResponseMetadata", {}).get("HTTPStatusCode") == 412
    )


def _current_owner(s3, bucket: str, receipt_key: str) -> str:  # noqa: ANN001
    """Best-effort current owner of the attempt claim (for error messages)."""
    with contextlib.suppress(Exception):
        current = json.loads(s3.get_object(Bucket=bucket, Key=receipt_key)["Body"].read())
        return str(current.get("execution_id") or "unknown")
    return "unknown"


def _claim_attempt(
    s3,  # noqa: ANN001
    bucket: str,
    prefix: str,
    receipt_key: str,
    receipt: dict,
    conditional: bool,
    heartbeat,  # noqa: ANN001
) -> None:
    """Claim the attempt prefix; the receipt is the ownership record.

    The first claimant wins the If-None-Match PUT. A 412 loser reclaims only
    an abandoned claim (older than SHIZZLE_DISPATCH_STALE_SECONDS, via IfMatch
    on the observed ETag); every other non-owner outcome raises
    AttemptClaimedError so the RunPod job fails and the orchestrator retries
    under a fresh idempotency key (B12).
    """
    claim_put: dict = {
        "Bucket": bucket,
        "Key": receipt_key,
        "Body": json.dumps(receipt, separators=(",", ":")).encode("utf-8"),
        "ContentType": "application/json",
    }
    if conditional:
        claim_put["IfNoneMatch"] = "*"
    else:
        print(
            "[warn] SHIZZLE_CONDITIONAL_DISPATCH=0: unconditional receipt write; "
            "replay guard is the completion check only",
            flush=True,
        )
    try:
        s3.put_object(**claim_put)
        return
    except ClientError as err:
        if not conditional or not _is_precondition_failed(err):
            raise

    # 412: the prefix is already claimed. Reclaim only if abandoned.
    response = s3.get_object(Bucket=bucket, Key=receipt_key)
    etag = response["ETag"]
    existing = json.loads(response["Body"].read())
    try:
        age = _utcnow() - datetime.fromisoformat(existing["claimed_at"])
    except (KeyError, TypeError, ValueError):
        age = None
    if age is None or age <= timedelta(seconds=_DISPATCH_STALE_SECONDS):
        raise AttemptClaimedError(
            f"attempt {prefix} is claimed by execution "
            f"{existing.get('execution_id', '?')} "
            f"(claimed_at {existing.get('claimed_at', '?')}); claim is not stale"
        )
    heartbeat("dispatch: reclaiming abandoned claim")
    reclaim_put: dict = {
        "Bucket": bucket,
        "Key": receipt_key,
        "Body": json.dumps(
            {**receipt, "claimed_at": _utcnow().isoformat()}, separators=(",", ":")
        ).encode("utf-8"),
        "ContentType": "application/json",
        "IfMatch": etag,
    }
    try:
        s3.put_object(**reclaim_put)
    except ClientError as err:
        if not _is_precondition_failed(err):
            raise
        owner = _current_owner(s3, bucket, receipt_key)
        raise AttemptClaimedError(
            f"attempt {prefix} was reclaimed by another worker first "
            f"(owner execution {owner})"
        ) from err


def handler(job: dict) -> dict:
    """RunPod serverless handler: S3 source in, lossless-stem-v1 package in S3 out."""
    import runpod

    def heartbeat(msg: str) -> None:
        with contextlib.suppress(Exception):
            runpod.serverless.progress_update(job, msg)

    params = job.get("input", {})
    runpod_job_id = str(job.get("id", "")).strip()
    track_id = params["track_id"]
    generation = int(params.get("generation", 1))
    idempotency_key = str(params.get("idempotency_key", "")).strip()
    if not runpod_job_id or not idempotency_key:
        raise ValueError("RunPod id and idempotency_key are required")
    bucket = params["bucket"]
    input_key = params["input_key"]
    base_prefix = params.get(
        "output_prefix", f"tracks/{track_id}/{generation}/separation/"
    )
    prefix = _attempt_prefix(base_prefix, idempotency_key)

    s3 = create_s3_client()
    handoff_key = f"{prefix}/handoff.json"
    receipt_key = f"{prefix}/dispatch.json"
    execution_id = str(uuid.uuid4())

    # Replay guard, completion check (#32): a visible handoff means this
    # attempt already crossed the interface. A redelivery returns the stored
    # completion metadata and writes nothing (A1/A6).
    try:
        stored = json.loads(s3.get_object(Bucket=bucket, Key=handoff_key)["Body"].read())
    except ClientError as err:
        if not _is_not_found(err):
            raise
    else:
        heartbeat("dispatch: attempt already complete")
        return {
            "status": "COMPLETED",
            "interface": stored.get("interface", "lossless-stem-v1"),
            "track_id": track_id,
            "generation": generation,
            "package_prefix": prefix,
            "sample_count": stored.get("separation", {}).get("sample_count", 0),
            "uploads": 0,
            "timings": {},
        }

    # Replay guard, ownership claim (#32): the conditional receipt PUT admits
    # exactly one owner per attempt prefix; any non-owner raises so the RunPod
    # job fails and the orchestrator retries under a fresh idempotency key
    # (B12). An abandoned claim is reclaimed, never reported as completion.
    receipt = {
        "runpod_job_id": runpod_job_id,
        "idempotency_key": idempotency_key,
        "track_id": str(track_id),
        "generation": generation,
        "package_prefix": prefix,
        "claimed_at": _utcnow().isoformat(),
        "execution_id": execution_id,
    }
    heartbeat(f"dispatch: recording {runpod_job_id}")
    _claim_attempt(s3, bucket, prefix, receipt_key, receipt, _CONDITIONAL_DISPATCH, heartbeat)

    # Every dispatch writes beneath its own immutable prefix. A completed
    # attempt is never rewritten (guards above), so an older worker can
    # neither replace a newer receipt nor mutate stems beneath a completed
    # handoff marker. handoff.json remains the last-write completion marker
    # for this attempt only.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = tmp_path / Path(input_key).name
        heartbeat(f"acquire: downloading s3://{bucket}/{input_key}")
        download_source(s3, bucket, input_key, source, heartbeat)

        handoff = run(
            source, tmp_path,
            track_id=track_id, generation=generation,
            source_key=input_key, worker_image=WORKER_IMAGE,
            heartbeat=heartbeat,
        )

        uploads = []
        for i, role in enumerate(ROLES, 1):
            key = f"{prefix}/stems/{role}.wav"
            heartbeat(f"upload: {role}.wav ({i}/6) -> {key}")
            uploads.append(
                upload_file(
                    s3, bucket, key, tmp_path / "stems" / f"{role}.wav", heartbeat
                )
            )

        # handoff.json is written LAST: its presence means the package crossed
        # the interface. A dead worker leaves no handoff and therefore nothing
        # downstream will consume.
        handoff_path = tmp_path / "handoff.json"
        handoff_path.write_text(json.dumps(_clean(handoff), indent=2))

        # Replay guard, ownership re-check (#32): a late predecessor that lost
        # its claim (stale reclaim) must not publish a handoff over the
        # reclaimer's stems.
        if _CONDITIONAL_DISPATCH:
            current = json.loads(s3.get_object(Bucket=bucket, Key=receipt_key)["Body"].read())
            if current.get("execution_id") != execution_id:
                raise AttemptClaimedError(
                    f"attempt {prefix} is now owned by execution "
                    f"{current.get('execution_id', '?')}; refusing to write handoff"
                )

        heartbeat(f"handoff: writing {handoff_key} (package complete)")
        handoff_put: dict = {
            "Bucket": bucket,
            "Key": handoff_key,
            "Body": handoff_path.read_bytes(),
            "ContentType": "application/json",
        }
        if _CONDITIONAL_DISPATCH:
            handoff_put["IfNoneMatch"] = "*"
        try:
            s3.put_object(**handoff_put)
        except ClientError as err:
            if _CONDITIONAL_DISPATCH and _is_precondition_failed(err):
                owner = _current_owner(s3, bucket, receipt_key)
                raise AttemptClaimedError(
                    f"attempt {prefix} already has a handoff "
                    f"(owner execution {owner})"
                ) from err
            raise

    return {
        "status": "COMPLETED",
        "interface": handoff["interface"],
        "track_id": track_id,
        "generation": generation,
        "package_prefix": prefix,
        "sample_count": handoff["separation"]["sample_count"],
        "uploads": len(uploads) + 1,
        "timings": handoff.get("_timings", {}),
    }


def run_local(input_path: str, output_dir: str, track_id: str | None = None) -> dict:
    """File in, package on disk out. The MP4-on-this-machine proving path."""
    source = Path(input_path)
    if not source.exists():
        raise FileNotFoundError(source)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    handoff = run(
        source, out,
        track_id=track_id or str(uuid.uuid4()),
        generation=1,
        source_key=f"local/{source.name}",
        worker_image=WORKER_IMAGE,
        heartbeat=lambda msg: print(f"[phase] {msg}", flush=True),
    )
    (out / "handoff.json").write_text(json.dumps(_clean(handoff), indent=2))
    print(f"[done] package at {out} (timings: {handoff.get('_timings')})", flush=True)
    return handoff


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--local":
        if len(sys.argv) < 4:
            print(__doc__)
            sys.exit(2)
        tid = sys.argv[sys.argv.index("--track-id") + 1] if "--track-id" in sys.argv else None
        run_local(sys.argv[2], sys.argv[3], tid)
    else:
        import runpod

        runpod.serverless.start({"handler": handler})

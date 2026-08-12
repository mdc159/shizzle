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
import json
import os
import sys
import tempfile
import uuid
from pathlib import Path

from lossless_worker import ROLES, run
from s3_ops import create_s3_client, download_source, upload_file

WORKER_IMAGE = os.getenv("WORKER_IMAGE", "unknown")


def _clean(handoff: dict) -> dict:
    """Strip private keys; the uploaded document is schema-strict."""
    return {k: v for k, v in handoff.items() if not k.startswith("_")}


def handler(job: dict) -> dict:
    """RunPod serverless handler: S3 source in, lossless-stem-v1 package in S3 out."""
    import runpod

    def heartbeat(msg: str) -> None:
        with contextlib.suppress(Exception):
            runpod.serverless.progress_update(job, msg)

    params = job.get("input", {})
    track_id = params["track_id"]
    generation = int(params.get("generation", 1))
    bucket = params["bucket"]
    input_key = params["input_key"]
    prefix = params.get(
        "output_prefix", f"tracks/{track_id}/{generation}/separation/"
    ).rstrip("/")

    s3 = create_s3_client()
    handoff_key = f"{prefix}/handoff.json"
    s3.delete_object(Bucket=bucket, Key=handoff_key)
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
        heartbeat(f"handoff: writing {handoff_key} (package complete)")
        upload_file(s3, bucket, handoff_key, handoff_path, heartbeat)

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

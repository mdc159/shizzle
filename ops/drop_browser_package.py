#!/usr/bin/env python
"""Drop a completed shizzle-browser-v1 package into the S3 drop-box (issue #45).

Uploads an already-finished package (six AAC stems, silent video, v3 manifest)
under ``s3://{bucket}/imports/{source_ref}/`` for the VPS ingest
(``python -m shizzle_server.publish.browser_import``) to validate, publish and
register. Media goes up first; ``manifest.json`` goes up LAST — its presence
is the completion marker, exactly like the worker's handoff (A1/C2).

The local candidate is verified against the manifest's ``integrity.objects``
(bytes + sha256) BEFORE any upload, so a corrupt candidate never half-drops.
Uploads are restartable: an object whose remote size already matches is
skipped. With ``--wait``, polls for the ingest's ``result.json``.

Client-facing contract: ``docs/contributing-completed-media.md``.

Usage:
    python ops/drop_browser_package.py --candidate DIR --source-ref REF \
        [--bucket karaoke-pimpshizzle] [--region us-east-1] [--wait]

``AWS_ENDPOINT_URL`` / ``AWS_ENDPOINT_URL_S3`` are popped before the client is
created (a global R2 override on this machine would silently point every call
at the wrong provider — see ``library/src/shizzle_server/api/media.py``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

SOURCE_REF_RE = re.compile(r"^(youtube|sha256)-[A-Za-z0-9_-]{6,128}$")
_SHA_RE = re.compile(r"[0-9a-f]{64}")
STEMS = ("vocals", "drums", "bass", "guitar", "piano", "shizzle")
CONTENT_TYPES = {".m4a": "audio/mp4", ".mp4": "video/mp4", ".json": "application/json"}
DEFAULT_BUCKET = "karaoke-pimpshizzle"
RESULT_NAME = "result.json"
POLL_SECONDS = 10


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, allow_nan=False))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_manifest(manifest: Any) -> dict[str, tuple[int, str]] | str:
    """Structural gate mirroring the ingest: the declared objects or a problem.

    Runs before the boto3 client is created, so a malformed manifest never
    costs an S3 call: ``integrity.objects`` must declare exactly the seven
    canonical files, each once, with a positive integer ``bytes`` and a
    lowercase 64-hex ``sha256``.
    """
    if not isinstance(manifest, dict):
        return "manifest.json must contain a JSON object"
    integrity = manifest.get("integrity")
    objects = integrity.get("objects") if isinstance(integrity, dict) else None
    if not isinstance(objects, list):
        return "integrity.objects must be a list"
    declared: dict[str, tuple[int, str]] = {}
    for entry in objects:
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            return "every integrity.objects entry must be an object declaring a file"
        file = entry["file"]
        if file in declared:
            return f"{file}: declared more than once in integrity.objects"
        size, sha = entry.get("bytes"), entry.get("sha256")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            return f"{file}: bytes must be a positive integer"
        if not isinstance(sha, str) or _SHA_RE.fullmatch(sha) is None:
            return f"{file}: sha256 must be a lowercase 64-char hex digest"
        declared[file] = (size, sha)
    wanted = {"video.mp4"} | {f"stems/{role}.m4a" for role in STEMS}
    if set(declared) != wanted:
        missing = ", ".join(sorted(wanted - set(declared))) or "-"
        extra = ", ".join(sorted(set(declared) - wanted)) or "-"
        return (
            "integrity.objects must declare exactly the seven canonical files "
            f"(video.mp4 + stems/{{id}}.m4a for {', '.join(STEMS)}); "
            f"missing: {missing}; unexpected: {extra}"
        )
    return declared


def _verify_local(candidate: Path, objects: dict[str, tuple[int, str]]) -> str | None:
    """Return a problem string when a local file disagrees with the manifest."""
    for file, (size, sha) in sorted(objects.items()):
        local = candidate / file
        if not local.is_file():
            return f"{file}: missing from candidate"
        if local.stat().st_size != size:
            return f"{file}: local size {local.stat().st_size} != declared {size}"
        if _sha256_file(local) != sha:
            return f"{file}: local sha256 does not match the declared digest"
    return None


def _remote_size_matches(s3: Any, bucket: str, key: str, size: int) -> bool:
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code", "")) in ("404", "NoSuchKey", "NotFound"):
            return False
        raise
    return int(head.get("ContentLength", -1)) == size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--wait", action="store_true", help="poll for the ingest result.json")
    parser.add_argument("--wait-seconds", type=int, default=900)
    args = parser.parse_args(argv)

    if SOURCE_REF_RE.fullmatch(args.source_ref) is None:
        _emit({"status": "invalid-source-ref", "sourceRef": args.source_ref})
        return 2

    manifest_path = args.candidate / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        _emit({"status": "invalid-manifest", "message": str(exc)})
        return 2
    validated = _validate_manifest(manifest)
    if isinstance(validated, str):
        _emit({"status": "invalid-manifest", "message": validated})
        return 2
    objects = validated

    # Abort before any upload on the first local/manifest disagreement.
    problem = _verify_local(args.candidate, objects)
    if problem is not None:
        _emit({"status": "candidate-mismatch", "message": problem})
        return 2

    # Machine gotcha: a global AWS_ENDPOINT_URL points at Cloudflare R2.
    os.environ.pop("AWS_ENDPOINT_URL", None)
    os.environ.pop("AWS_ENDPOINT_URL_S3", None)
    try:
        s3 = boto3.client("s3", region_name=args.region)
    except Exception as exc:  # boto3/botocore failures of any class
        _emit({"status": "client-failed", "message": f"{type(exc).__name__}: {exc}"[:400]})
        return 1

    prefix = f"imports/{args.source_ref}/"

    def upload(file: str, size: int) -> None:
        if _remote_size_matches(s3, args.bucket, f"{prefix}{file}", size):
            return  # restartable: already dropped with the same size
        s3.upload_file(
            str(args.candidate / file),
            args.bucket,
            f"{prefix}{file}",
            ExtraArgs={
                "ContentType": CONTENT_TYPES.get(Path(file).suffix, "application/octet-stream"),
                "ChecksumAlgorithm": "SHA256",
            },
        )

    try:
        for file in sorted(objects):  # the seven media files
            upload(file, objects[file][0])
        # manifest LAST, ALWAYS: its presence marks the drop complete, so it
        # never goes through the size-match skip path — even a byte-identical
        # remote manifest is re-put as the final call.
        s3.upload_file(
            str(manifest_path),
            args.bucket,
            f"{prefix}manifest.json",
            ExtraArgs={"ContentType": "application/json", "ChecksumAlgorithm": "SHA256"},
        )
    except Exception as exc:  # boto3/botocore failures of any class
        _emit({"status": "upload-failed", "message": f"{type(exc).__name__}: {exc}"[:400]})
        return 1

    if not args.wait:
        _emit({"status": "uploaded", "sourceRef": args.source_ref, "prefix": prefix})
        return 0

    deadline = time.monotonic() + args.wait_seconds
    while time.monotonic() < deadline:
        try:
            body = s3.get_object(Bucket=args.bucket, Key=f"{prefix}{RESULT_NAME}")["Body"].read()
            result = json.loads(body)
        except Exception:  # absent, unreachable, or malformed result: keep polling
            time.sleep(POLL_SECONDS)
            continue
        _emit(result)
        status = result.get("status")
        if status == "rejected":
            return 2
        return 0 if status in ("published", "already-published", "would-publish") else 1
    _emit({"status": "wait-timeout", "sourceRef": args.source_ref})
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env -S uv run --script
"""Freeze and fully audit the active Shizzle delivery library.

Run this on the VPS/API container so all media processing remains in the cloud.
It downloads one immutable generation at a time to temporary VPS storage,
hashes and fully decodes every required object, then removes the scratch copy.
The active objects are never modified.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_SRC = REPO_ROOT / "library" / "src"
sys.path.insert(0, str(SERVER_SRC))

from shizzle_server.db import create_engine, create_session_factory  # noqa: E402
from shizzle_server.db.repository import TrackRepository  # noqa: E402
from shizzle_server.publish.delivery_profile import (  # noqa: E402
    CANONICAL_STEM_IDS,
    MAX_TOTAL_AVERAGE_BITRATE,
    PROFILE_ID,
    STEM_INTER_DURATION_TOLERANCE_SEC,
    ProfileIssue,
    canonical_stem_id,
    evaluate_manifest,
    has_errors,
)
from shizzle_server.publish.media_audit import (  # noqa: E402
    MediaAuditError,
    audit_audio_file,
    audit_video_file,
)

DEFAULT_BUCKET = "karaoke-pimpshizzle"


@dataclass(frozen=True)
class TrackRef:
    id: str
    title: str
    artist: str
    duration: float
    generation: int
    s3_prefix: str
    manifest_key: str
    integrity: dict[str, Any] | None


async def load_tracks(database_url: str, selected: set[str]) -> list[TrackRef]:
    engine = create_engine(database_url)
    try:
        repo = TrackRepository(create_session_factory(engine))
        rows = await repo.list_tracks()
        return [
            TrackRef(
                id=str(row.id),
                title=row.title,
                artist=row.artist,
                duration=row.duration_seconds,
                generation=row.generation,
                s3_prefix=row.s3_prefix.rstrip("/"),
                manifest_key=row.manifest_key,
                integrity=row.integrity,
            )
            for row in rows
            if not selected or str(row.id) in selected
        ]
    finally:
        await engine.dispose()


def _s3_client(region: str, endpoint_url: str | None) -> Any:
    # This Windows workstation has historically carried an R2-wide endpoint
    # override. The real Shizzle bucket is AWS S3; never inherit that override.
    os.environ.pop("AWS_ENDPOINT_URL", None)
    os.environ.pop("AWS_ENDPOINT_URL_S3", None)
    return boto3.client("s3", region_name=region, endpoint_url=endpoint_url)


def _not_found(exc: ClientError) -> bool:
    return str(exc.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NotFound"}


def head_object(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        head = s3.head_object(Bucket=bucket, Key=key, ChecksumMode="ENABLED")
    except ClientError as exc:
        if _not_found(exc):
            return None
        try:
            head = s3.head_object(Bucket=bucket, Key=key)
        except ClientError as plain_exc:
            if _not_found(plain_exc):
                return None
            raise
    checksum = head.get("ChecksumSHA256")
    checksum_hex = None
    if checksum:
        checksum_hex = base64.b64decode(checksum).hex()
    return {
        "bytes": int(head["ContentLength"]),
        "content_type": head.get("ContentType"),
        "etag": str(head.get("ETag", "")).strip('"'),
        "checksum_sha256": checksum_hex,
        "last_modified": head.get("LastModified").astimezone(UTC).isoformat()
        if head.get("LastModified")
        else None,
    }


def load_manifest(s3: Any, bucket: str, key: str) -> tuple[dict[str, Any], bytes]:
    obj = s3.get_object(Bucket=bucket, Key=key)
    body = obj["Body"].read()
    return json.loads(body), body


def object_refs(track: TrackRef, manifest: dict[str, Any]) -> list[dict[str, str]]:
    refs = [{"kind": "video", "role": "video", "file": str(manifest.get("video", ""))}]
    for stem in manifest.get("stems", []):
        if not isinstance(stem, dict):
            continue
        raw_role = str(stem.get("id", ""))
        refs.append(
            {
                "kind": "stem",
                "role": canonical_stem_id(raw_role),
                "source_role": raw_role,
                "file": str(stem.get("file", "")),
            }
        )
    for ref in refs:
        ref["key"] = f"{track.s3_prefix}/{ref['file'].lstrip('/')}"
    return refs


def freeze_track(s3: Any, bucket: str, track: TrackRef) -> dict[str, Any]:
    manifest, body = load_manifest(s3, bucket, track.manifest_key)
    refs = object_refs(track, manifest)
    objects = []
    for ref in refs:
        objects.append({**ref, "head": head_object(s3, bucket, ref["key"])})
    return {
        **asdict(track),
        "manifest_sha256": __import__("hashlib").sha256(body).hexdigest(),
        "manifest": manifest,
        "objects": objects,
    }


def _probe_bitrate(audit: dict[str, Any]) -> int:
    streams = audit.get("probe", {}).get("streams", [])
    stream = streams[0] if streams else {}
    raw = stream.get("bit_rate") or audit.get("probe", {}).get("format", {}).get("bit_rate")
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return 0


def _probe_duration(audit: dict[str, Any], kind: str) -> float | None:
    streams = audit.get("probe", {}).get("streams", [])
    stream = next((item for item in streams if item.get("codec_type") == kind), None)
    raw = (stream or {}).get("duration") or audit.get("probe", {}).get("format", {}).get("duration")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def audit_track(
    s3: Any,
    bucket: str,
    frozen: dict[str, Any],
    *,
    scratch_root: Path,
    decode: bool,
) -> dict[str, Any]:
    track_dir = scratch_root / frozen["id"] / str(frozen["generation"])
    track_dir.mkdir(parents=True, exist_ok=True)
    manifest = frozen["manifest"]
    expected_duration = float(manifest.get("duration") or frozen["duration"])
    issues = evaluate_manifest(manifest)
    audits: list[dict[str, Any]] = []

    expected_roles = set(CANONICAL_STEM_IDS)
    actual_roles = {obj["role"] for obj in frozen["objects"] if obj["kind"] == "stem"}
    if actual_roles != expected_roles:
        issues.append(
            ProfileIssue(
                "object-role-set",
                f"object roles {sorted(actual_roles)} do not equal {sorted(expected_roles)}",
            )
        )

    for obj in frozen["objects"]:
        artifact = obj["file"]
        if not artifact or obj["head"] is None:
            issues.append(ProfileIssue("object-missing", f"missing S3 object {obj['key']}", artifact))
            continue
        destination = track_dir / artifact.replace("/", "__")
        s3.download_file(bucket, obj["key"], str(destination))
        try:
            if obj["kind"] == "video":
                result = audit_video_file(
                    destination,
                    artifact=artifact,
                    expected_duration=expected_duration,
                    decode=decode,
                )
            else:
                result = audit_audio_file(
                    destination,
                    artifact=artifact,
                    expected_duration=expected_duration,
                    preserve_existing_lossy=True,
                    decode=decode,
                )
            result["key"] = obj["key"]
            result["content_type"] = obj["head"]["content_type"]
            stored = obj["head"].get("checksum_sha256")
            if stored and stored != result["sha256"]:
                result["issues"].append(
                    ProfileIssue(
                        "object-checksum",
                        f"stored SHA-256 {stored} does not match decoded object {result['sha256']}",
                        artifact,
                    ).as_dict()
                )
                result["passed"] = False
            audits.append(result)
            issues.extend(ProfileIssue(**item) for item in result["issues"])
        except MediaAuditError as exc:
            issues.append(ProfileIssue("media-audit-error", str(exc), artifact))
        finally:
            destination.unlink(missing_ok=True)

    total_bitrate = sum(_probe_bitrate(item) for item in audits)
    stem_durations = [
        duration
        for item in audits
        if item["artifact"] != "video.mp4"
        and (duration := _probe_duration(item, "audio")) is not None
    ]
    if stem_durations and max(stem_durations) - min(stem_durations) > STEM_INTER_DURATION_TOLERANCE_SEC:
        issues.append(
            ProfileIssue(
                "stem-duration-skew",
                f"six-stem duration spread {max(stem_durations) - min(stem_durations):.6f}s "
                f"exceeds {STEM_INTER_DURATION_TOLERANCE_SEC:.3f}s",
            )
        )
    if total_bitrate > MAX_TOTAL_AVERAGE_BITRATE:
        issues.append(
            ProfileIssue(
                "track-bitrate-budget",
                f"total average bitrate {total_bitrate} exceeds {MAX_TOTAL_AVERAGE_BITRATE}",
            )
        )
    return {
        "id": frozen["id"],
        "title": frozen["title"],
        "generation": frozen["generation"],
        "delivery_profile": manifest.get("delivery_profile"),
        "total_average_bitrate_bps": total_bitrate,
        "objects": audits,
        "issues": [issue.as_dict() for issue in issues],
        "passed": not has_errors(issues) and len(audits) == 7,
    }


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--bucket", default=os.environ.get("S3_BUCKET", DEFAULT_BUCKET))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--endpoint-url", default=None)
    parser.add_argument("--track-id", action="append", default=[])
    parser.add_argument(
        "--generation",
        type=int,
        help="audit this immutable generation instead of the active pointer (one track only)",
    )
    parser.add_argument("--snapshot-output", type=Path, required=True)
    parser.add_argument("--report-output", type=Path)
    parser.add_argument(
        "--snapshot-only",
        action="store_true",
        help="freeze DB/manifest/HEAD evidence without downloading or decoding media",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="download/hash/probe/keyframe-check but skip full FFmpeg decode",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.database_url:
        print("DATABASE_URL or --database-url is required", file=sys.stderr)
        return 2
    selected = {str(uuid.UUID(value)) for value in args.track_id}
    tracks = asyncio.run(load_tracks(args.database_url, selected))
    if args.generation is not None:
        if len(tracks) != 1:
            print("--generation requires exactly one selected track", file=sys.stderr)
            return 2
        track = tracks[0]
        prefix = f"tracks/{track.id}/{args.generation}"
        tracks = [
            replace(
                track,
                generation=args.generation,
                s3_prefix=prefix,
                manifest_key=f"{prefix}/manifest.json",
            )
        ]
    s3 = _s3_client(args.region, args.endpoint_url)
    frozen = [freeze_track(s3, args.bucket, track) for track in tracks]
    generated_at = datetime.now(UTC).isoformat()
    snapshot = {
        "schema": "shizzle-library-snapshot-v1",
        "generated_at": generated_at,
        "profile": PROFILE_ID,
        "bucket": args.bucket,
        "track_count": len(frozen),
        "media_object_count": sum(len(track["objects"]) for track in frozen),
        "total_duration_seconds": sum(float(track["duration"]) for track in frozen),
        "tracks": frozen,
    }
    write_json(args.snapshot_output, snapshot)
    print(
        f"snapshot: {len(frozen)} tracks, {snapshot['media_object_count']} media objects -> "
        f"{args.snapshot_output}"
    )
    if args.snapshot_only:
        return 0
    if not args.report_output:
        print("--report-output is required unless --snapshot-only is set", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="shizzle-audit-") as scratch:
        reports = [
            audit_track(
                s3,
                args.bucket,
                track,
                scratch_root=Path(scratch),
                decode=not args.metadata_only,
            )
            for track in frozen
        ]
    report = {
        "schema": "shizzle-library-audit-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "snapshot_generated_at": generated_at,
        "profile": PROFILE_ID,
        "full_decode": not args.metadata_only,
        "track_count": len(reports),
        "passed_count": sum(1 for item in reports if item["passed"]),
        "failed_count": sum(1 for item in reports if not item["passed"]),
        "tracks": reports,
    }
    write_json(args.report_output, report)
    print(
        f"audit: {report['passed_count']} passed, {report['failed_count']} failed -> "
        f"{args.report_output}"
    )
    return 0 if report["failed_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

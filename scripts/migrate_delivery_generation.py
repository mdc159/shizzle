#!/usr/bin/env -S uv run --script
"""Build, publish, activate, or roll back audited immutable delivery generations."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_SRC = REPO_ROOT / "server" / "src"
sys.path.insert(0, str(SERVER_SRC))

from shizzle_server.db import create_engine, create_session_factory  # noqa: E402
from shizzle_server.db.repository import TrackRepository  # noqa: E402
from shizzle_server.delivery_profile import PROFILE_ID  # noqa: E402
from shizzle_server.library_migration import (  # noqa: E402
    build_migrated_manifest,
    decide_migration,
    video_encode_command,
)
from shizzle_server.media_audit import audit_video_file, sha256_file  # noqa: E402
from shizzle_server.publish import (  # noqa: E402
    ChecksumPolicy,
    Publisher,
    StagedObject,
    generation_prefix,
    manifest_key,
    staging_prefix,
)

DEFAULT_BUCKET = "karaoke-pimpshizzle"


def load_json(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def s3_client(region: str) -> Any:
    os.environ.pop("AWS_ENDPOINT_URL", None)
    os.environ.pop("AWS_ENDPOINT_URL_S3", None)
    return boto3.client("s3", region_name=region)


def upload_file(
    s3: Any,
    bucket: str,
    key: str,
    path: Path,
    content_type: str,
    *,
    metadata: dict[str, str] | None = None,
) -> None:
    extra: dict[str, Any] = {
        "ContentType": content_type,
        "ChecksumAlgorithm": "SHA256",
    }
    if metadata:
        extra["Metadata"] = metadata
    s3.upload_file(
        str(path),
        bucket,
        key,
        ExtraArgs=extra,
    )


def _find_by_id(items: list[dict[str, Any]], track_id: str) -> dict[str, Any]:
    try:
        return next(item for item in items if item["id"] == track_id)
    except StopIteration as exc:
        raise ValueError(f"track {track_id} is absent from evidence") from exc


def _object_audit(audit: dict[str, Any], artifact: str) -> dict[str, Any]:
    try:
        return next(item for item in audit["objects"] if item["artifact"] == artifact)
    except StopIteration as exc:
        raise ValueError(f"audit is missing {artifact}") from exc


def plan_tracks(
    snapshot: dict[str, Any], report: dict[str, Any], selected: set[str]
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    planned = []
    for track in snapshot["tracks"]:
        if selected and track["id"] not in selected:
            continue
        planned.append((track, _find_by_id(report["tracks"], track["id"])))
    return planned


def build_and_publish(
    *,
    s3: Any,
    bucket: str,
    publisher: Publisher,
    track: dict[str, Any],
    audit: dict[str, Any],
    audit_sha256: str,
    publish: bool,
    common_gain_db: float | None = None,
    common_gain_reason: str | None = None,
) -> dict[str, Any]:
    decision = decide_migration(audit)
    source_generation = int(track["generation"])
    new_generation = source_generation + 1
    outcome: dict[str, Any] = {
        "id": track["id"],
        "title": track["title"],
        "source_generation": source_generation,
        "generation": new_generation,
        "video_action": decision.video_action,
        "reason": decision.reason,
        "status": "planned",
        "common_gain_db": common_gain_db,
        "common_gain_reason": common_gain_reason,
    }
    if not publish:
        return outcome
    if publisher.is_published(track["id"], new_generation):
        existing = json.loads(
            s3.get_object(Bucket=bucket, Key=manifest_key(track["id"], new_generation))["Body"].read()
        )
        integrity = existing.get("integrity", {})
        if (
            integrity.get("source") != "audited-library-migration"
            or integrity.get("source_generation") != source_generation
            or integrity.get("audit_sha256") != audit_sha256
        ):
            raise ValueError(
                f"generation {new_generation} already exists but does not match this evidence"
            )
        outcome.update(status="already-published", manifest=existing)
        return outcome

    stage = staging_prefix(track["id"], new_generation)
    source_objects = {item["file"]: item for item in track["objects"]}
    reported: list[StagedObject] = []
    provenance: list[dict[str, Any]] = []
    expected_duration = float(track["manifest"].get("duration") or track["duration"])

    with tempfile.TemporaryDirectory(prefix=f"shizzle-migrate-{track['id']}-") as scratch_name:
        scratch = Path(scratch_name)
        source_video = source_objects[track["manifest"]["video"]]
        video_audit = _object_audit(audit, track["manifest"]["video"])
        if decision.video_action == "copy":
            size = int(source_video["head"]["bytes"])
            publisher.copy_object(
                source_video["key"], f"{stage}video.mp4", size, content_type="video/mp4"
            )
            video_sha = video_audit["sha256"]
            video_size = size
        else:
            input_path = scratch / "source-video.mp4"
            output_path = scratch / "video.mp4"
            staged_video_key = f"{stage}video.mp4"
            staged_head = None
            with contextlib.suppress(Exception):
                staged_head = s3.head_object(Bucket=bucket, Key=staged_video_key)
            staged_metadata = (staged_head or {}).get("Metadata", {})
            resume_staged = (
                staged_metadata.get("source-sha256") == video_audit["sha256"]
                and staged_metadata.get("profile") == PROFILE_ID
            )
            command = video_encode_command(input_path, output_path)
            if resume_staged:
                s3.download_file(bucket, staged_video_key, str(output_path))
                outcome["resumed_staged_video"] = True
            else:
                s3.download_file(bucket, source_video["key"], str(input_path))
                result = subprocess.run(
                    command, capture_output=True, text=True, timeout=3600, check=False
                )
                if result.returncode != 0:
                    raise RuntimeError(f"video encode failed: {result.stderr[-2000:]}")
            encoded_audit = audit_video_file(
                output_path,
                artifact="video.mp4",
                expected_duration=expected_duration,
                decode=True,
            )
            if not encoded_audit["passed"]:
                raise RuntimeError(f"encoded video failed gates: {encoded_audit['issues']}")
            if not resume_staged:
                upload_file(
                    s3,
                    bucket,
                    staged_video_key,
                    output_path,
                    "video/mp4",
                    metadata={
                        "source-sha256": video_audit["sha256"],
                        "profile": PROFILE_ID,
                    },
                )
            video_sha = encoded_audit["sha256"]
            video_size = int(encoded_audit["bytes"])
            outcome["video_command"] = command
            outcome["encoded_video_audit"] = encoded_audit
        reported.append(StagedObject("video.mp4", video_size, video_sha))
        provenance.append(
            {
                "file": "video.mp4",
                "action": decision.video_action,
                "source_key": source_video["key"],
                "source_sha256": video_audit["sha256"],
                "sha256": video_sha,
                "bytes": video_size,
            }
        )

        for stem in track["manifest"]["stems"]:
            source_file = str(stem["file"])
            source_obj = source_objects[source_file]
            source_audit = _object_audit(audit, source_file)
            role = "shizzle" if str(stem.get("id")) == "other" else str(stem.get("id"))
            destination_file = f"stems/{role}.m4a"
            size = int(source_obj["head"]["bytes"])
            publisher.copy_object(
                source_obj["key"], f"{stage}{destination_file}", size, content_type="audio/mp4"
            )
            reported.append(StagedObject(destination_file, size, source_audit["sha256"]))
            provenance.append(
                {
                    "file": destination_file,
                    "action": "copy",
                    "source_key": source_obj["key"],
                    "source_sha256": source_audit["sha256"],
                    "sha256": source_audit["sha256"],
                    "bytes": size,
                }
            )

        manifest = build_migrated_manifest(
            track["manifest"],
            track_id=track["id"],
            source_generation=source_generation,
            generation=new_generation,
            audit_sha256=audit_sha256,
            decision=decision,
            object_provenance=provenance,
            common_gain_db=common_gain_db,
            common_gain_reason=common_gain_reason,
        )
        manifest_path = scratch / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        manifest_sha = sha256_file(manifest_path)
        upload_file(s3, bucket, f"{stage}manifest.json", manifest_path, "application/json")
        reported.append(StagedObject("manifest.json", manifest_path.stat().st_size, manifest_sha))

    published = publisher.publish(track["id"], new_generation, reported)
    outcome.update(
        status="published",
        manifest=manifest,
        manifest_sha256=manifest_sha,
        publication={
            "s3_prefix": published.s3_prefix,
            "manifest_key": published.manifest_key,
            "bytes_copied": published.bytes_copied,
            "verification": published.verification.to_integrity()
            if published.verification
            else None,
        },
    )
    return outcome


async def activate_outcomes(database_url: str, outcomes: list[dict[str, Any]]) -> None:
    engine = create_engine(database_url)
    try:
        repo = TrackRepository(create_session_factory(engine))
        for outcome in outcomes:
            if outcome["status"] not in {"published", "already-published"}:
                continue
            manifest = outcome["manifest"]
            track_id = uuid.UUID(outcome["id"])
            await repo.activate_generation(
                track_id,
                expected_generation=outcome["source_generation"],
                generation=outcome["generation"],
                s3_prefix=generation_prefix(track_id, outcome["generation"]),
                manifest_key=manifest_key(track_id, outcome["generation"]),
                integrity=manifest["integrity"],
                detail={
                    "profile": PROFILE_ID,
                    "manifest_sha256": outcome.get("manifest_sha256"),
                    "video_action": outcome["video_action"],
                    "common_gain_db": outcome.get("common_gain_db"),
                    "common_gain_reason": outcome.get("common_gain_reason"),
                },
            )
            outcome["status"] = "activated"
    finally:
        await engine.dispose()


async def rollback(
    *,
    database_url: str,
    s3: Any,
    bucket: str,
    track_id: str,
    current_generation: int,
    target_generation: int,
    reason: str,
) -> dict[str, Any]:
    target_key = manifest_key(track_id, target_generation)
    manifest = json.loads(s3.get_object(Bucket=bucket, Key=target_key)["Body"].read())
    engine = create_engine(database_url)
    try:
        repo = TrackRepository(create_session_factory(engine))
        await repo.activate_generation(
            uuid.UUID(track_id),
            expected_generation=current_generation,
            generation=target_generation,
            s3_prefix=generation_prefix(track_id, target_generation),
            manifest_key=target_key,
            integrity=manifest.get("integrity") or {"source": "rollback-target"},
            event="rollback",
            detail={"reason": reason},
        )
    finally:
        await engine.dispose()
    return {
        "id": track_id,
        "status": "rolled-back",
        "from_generation": current_generation,
        "generation": target_generation,
        "reason": reason,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--audit-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--bucket", default=os.environ.get("S3_BUCKET", DEFAULT_BUCKET))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--track-id", action="append", default=[])
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--activate", action="store_true")
    parser.add_argument("--rollback-to-generation", type=int)
    parser.add_argument("--rollback-reason", default="verified rollback drill")
    parser.add_argument("--common-gain-db", type=float)
    parser.add_argument("--common-gain-reason")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.activate and not args.publish and args.rollback_to_generation is None:
        print("--activate requires --publish", file=sys.stderr)
        return 2
    if args.activate and not args.database_url:
        print("DATABASE_URL or --database-url is required for activation", file=sys.stderr)
        return 2
    snapshot = load_json(args.snapshot)
    report = load_json(args.audit_report)
    selected = {str(uuid.UUID(value)) for value in args.track_id}
    if args.common_gain_db is not None and len(selected) != 1:
        print("--common-gain-db requires exactly one --track-id", file=sys.stderr)
        return 2
    s3 = s3_client(args.region)

    if args.rollback_to_generation is not None:
        if len(selected) != 1 or not args.database_url:
            print("rollback requires exactly one --track-id and DATABASE_URL", file=sys.stderr)
            return 2
        track = _find_by_id(snapshot["tracks"], next(iter(selected)))
        outcome = asyncio.run(
            rollback(
                database_url=args.database_url,
                s3=s3,
                bucket=args.bucket,
                track_id=track["id"],
                current_generation=int(track["generation"]),
                target_generation=args.rollback_to_generation,
                reason=args.rollback_reason,
            )
        )
        outcomes = [outcome]
    else:
        audit_sha = file_sha256(args.audit_report)
        publisher = Publisher(s3, args.bucket, checksum_policy=ChecksumPolicy.AUTO)
        outcomes = [
            build_and_publish(
                s3=s3,
                bucket=args.bucket,
                publisher=publisher,
                track=track,
                audit=audit,
                audit_sha256=audit_sha,
                publish=args.publish,
                common_gain_db=args.common_gain_db,
                common_gain_reason=args.common_gain_reason,
            )
            for track, audit in plan_tracks(snapshot, report, selected)
        ]
        if args.activate:
            asyncio.run(activate_outcomes(args.database_url, outcomes))

    payload = {
        "schema": "shizzle-library-migration-v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "profile": PROFILE_ID,
        "published": args.publish,
        "activated": args.activate,
        "outcomes": outcomes,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome["status"]] = counts.get(outcome["status"], 0) + 1
    print(json.dumps(counts, sort_keys=True))
    print(f"report -> {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env -S uv run --script
"""Measure the decoded six-stem default mix for active cloud generations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3

from shizzle_server.audio_quality import build_six_stem_mix_filter, parse_mix_quality
from shizzle_server.db import create_engine, create_session_factory
from shizzle_server.db.repository import TrackRepository
from shizzle_server.delivery_profile import CANONICAL_STEM_IDS, canonical_stem_id


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


async def load_tracks(database_url: str, selected: set[str]) -> list[dict[str, Any]]:
    engine = create_engine(database_url)
    try:
        repo = TrackRepository(create_session_factory(engine))
        return [
            {
                "id": str(row.id),
                "title": row.title,
                "generation": row.generation,
                "manifest_key": row.manifest_key,
                "s3_prefix": row.s3_prefix.rstrip("/"),
            }
            for row in await repo.list_tracks()
            if not selected or str(row.id) in selected
        ]
    finally:
        await engine.dispose()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--bucket", default=os.environ.get("S3_BUCKET", "karaoke-pimpshizzle"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    parser.add_argument("--track-id", action="append", default=[])
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.database_url:
        raise SystemExit("DATABASE_URL or --database-url is required")
    selected = {str(uuid.UUID(value)) for value in args.track_id}
    tracks = asyncio.run(load_tracks(args.database_url, selected))
    os.environ.pop("AWS_ENDPOINT_URL", None)
    os.environ.pop("AWS_ENDPOINT_URL_S3", None)
    s3 = boto3.client("s3", region_name=args.region)
    report: dict[str, Any] = {
        "schema": "shizzle-audio-quality-v1",
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "track_count": len(tracks),
        "tracks": [],
    }
    write_json(args.output, report)

    for track in tracks:
        manifest = json.loads(
            s3.get_object(Bucket=args.bucket, Key=track["manifest_key"])["Body"].read()
        )
        by_role = {canonical_stem_id(str(stem["id"])): stem for stem in manifest.get("stems", [])}
        if set(by_role) != set(CANONICAL_STEM_IDS):
            raise RuntimeError(f"{track['id']} does not expose six canonical stems")
        with tempfile.TemporaryDirectory(prefix=f"shizzle-quality-{track['id']}-") as scratch:
            paths: list[Path] = []
            gain_dbs: list[float] = []
            for role in CANONICAL_STEM_IDS:
                stem = by_role[role]
                path = Path(scratch) / f"{role}.m4a"
                key = f"{track['s3_prefix']}/{str(stem['file']).lstrip('/')}"
                s3.download_file(args.bucket, key, str(path))
                paths.append(path)
                gain_dbs.append(float(stem.get("default_gain_db", 0.0)))
            command = ["ffmpeg", "-nostdin", "-hide_banner"]
            for path in paths:
                command.extend(["-i", str(path)])
            command.extend(
                [
                    "-filter_complex",
                    build_six_stem_mix_filter(gain_dbs),
                    "-map",
                    "[out_ebu]",
                    "-map",
                    "[out_stats]",
                    "-f",
                    "null",
                    "-",
                ]
            )
            measured = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=1800,
                check=False,
            )
            if measured.returncode != 0:
                raise RuntimeError(f"quality measurement failed: {measured.stderr[-4000:]}")
            quality = parse_mix_quality(measured.stderr)
        report["tracks"].append(
            {
                **track,
                "delivery_profile": manifest.get("delivery_profile"),
                "gain_dbs": gain_dbs,
                "measurement": quality.as_dict(),
            }
        )
        report["updated_at"] = datetime.now(UTC).isoformat()
        write_json(args.output, report)

    report.update(
        status="complete",
        passed_pre_limiter_count=sum(
            1 for track in report["tracks"] if track["measurement"]["passed_pre_limiter"]
        ),
        completed_at=datetime.now(UTC).isoformat(),
    )
    write_json(args.output, report)
    print(
        f"measured {len(tracks)} tracks; "
        f"{report['passed_pre_limiter_count']} pass before limiter -> {args.output}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

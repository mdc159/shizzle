#!/usr/bin/env -S uv run --script
"""Sequentially publish, full-audit, and activate delivery generations.

This coordinator is intentionally conservative: one track at a time, immutable
candidate first, complete S3 re-download/decode second, compare-and-swap
activation last. It checkpoints every phase and stops on the first failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from shizzle_server.db import create_engine, create_session_factory
from shizzle_server.db.repository import TrackRepository


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_checkpoint(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


async def active_generation(database_url: str, track_id: str) -> int | None:
    engine = create_engine(database_url)
    try:
        repo = TrackRepository(create_session_factory(engine))
        track = await repo.get(uuid.UUID(track_id))
        return track.generation if track is not None else None
    finally:
        await engine.dispose()


def run(command: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=7200,
        check=False,
        env=env,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--audit-report", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument("--track-id", action="append", default=[])
    parser.add_argument(
        "--migration-script",
        type=Path,
        default=Path("/tmp/migrate_delivery_generation.py"),
    )
    parser.add_argument(
        "--audit-script", type=Path, default=Path("/tmp/audit_delivery_library.py")
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.database_url:
        print("DATABASE_URL or --database-url is required", file=sys.stderr)
        return 2
    # Children resolve their database from DATABASE_URL in their environment;
    # propagating the resolved URL there keeps parent preflight/postflight and
    # every child on one database without exposing the DSN in child argv
    # (world-visible via process listings).
    child_env = {**os.environ, "DATABASE_URL": args.database_url}
    snapshot = load_json(args.snapshot)
    selected = {str(uuid.UUID(value)) for value in args.track_id}
    tracks = [
        track
        for track in snapshot["tracks"]
        if not selected or str(track["id"]) in selected
    ]
    if selected and selected != {str(track["id"]) for track in tracks}:
        missing = sorted(selected - {str(track["id"]) for track in tracks})
        print(f"selected tracks absent from snapshot: {missing}", file=sys.stderr)
        return 2

    payload: dict[str, Any] = {
        "schema": "shizzle-library-controlled-rollout-v1",
        "started_at": datetime.now(UTC).isoformat(),
        "updated_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "snapshot": str(args.snapshot),
        "baseline_audit": str(args.audit_report),
        "track_count": len(tracks),
        "outcomes": [],
    }
    write_checkpoint(args.output, payload)

    for track in tracks:
        track_id = str(track["id"])
        source_generation = int(track["generation"])
        candidate_generation = source_generation + 1
        stem = f"{track_id}-generation-{candidate_generation}"
        outcome: dict[str, Any] = {
            "id": track_id,
            "title": track["title"],
            "source_generation": source_generation,
            "generation": candidate_generation,
            "phase": "preflight",
            "status": "running",
        }
        payload["outcomes"].append(outcome)

        def checkpoint() -> None:
            payload["updated_at"] = datetime.now(UTC).isoformat()
            write_checkpoint(args.output, payload)

        try:
            current = asyncio.run(active_generation(args.database_url, track_id))
            outcome["active_generation_before"] = current
            if current not in {source_generation, candidate_generation}:
                raise RuntimeError(
                    f"active generation {current} is neither frozen source "
                    f"{source_generation} nor candidate {candidate_generation}"
                )

            publish_output = args.evidence_dir / f"{stem}-publish.json"
            outcome.update(phase="publishing", publish_report=str(publish_output))
            checkpoint()
            publish = run(
                [
                    sys.executable,
                    str(args.migration_script),
                    "--snapshot",
                    str(args.snapshot),
                    "--audit-report",
                    str(args.audit_report),
                    "--output",
                    str(publish_output),
                    "--track-id",
                    track_id,
                    "--publish",
                ],
                child_env,
            )
            outcome["publish_stdout"] = publish.stdout[-2000:]
            if publish.returncode != 0:
                raise RuntimeError(f"publish failed: {publish.stderr[-4000:]}")

            snapshot_output = args.evidence_dir / f"{stem}-snapshot.json"
            audit_output = args.evidence_dir / f"{stem}-audit.json"
            outcome.update(
                phase="full-audit",
                candidate_snapshot=str(snapshot_output),
                candidate_audit=str(audit_output),
            )
            checkpoint()
            audit = run(
                [
                    sys.executable,
                    str(args.audit_script),
                    "--track-id",
                    track_id,
                    "--generation",
                    str(candidate_generation),
                    "--snapshot-output",
                    str(snapshot_output),
                    "--report-output",
                    str(audit_output),
                ],
                child_env,
            )
            outcome["audit_stdout"] = audit.stdout[-2000:]
            if audit.returncode != 0:
                raise RuntimeError(
                    f"candidate full audit failed: {audit.stderr[-4000:]}"
                )
            report = load_json(audit_output)
            if report.get("failed_count") != 0 or report.get("passed_count") != 1:
                raise RuntimeError(
                    "candidate audit did not report exactly one passing track"
                )

            if current == source_generation:
                activation_output = args.evidence_dir / f"{stem}-activation.json"
                outcome.update(
                    phase="activation", activation_report=str(activation_output)
                )
                checkpoint()
                activation = run(
                    [
                        sys.executable,
                        str(args.migration_script),
                        "--snapshot",
                        str(args.snapshot),
                        "--audit-report",
                        str(args.audit_report),
                        "--output",
                        str(activation_output),
                        "--track-id",
                        track_id,
                        "--publish",
                        "--activate",
                    ],
                    child_env,
                )
                outcome["activation_stdout"] = activation.stdout[-2000:]
                if activation.returncode != 0:
                    raise RuntimeError(
                        f"activation failed: {activation.stderr[-4000:]}"
                    )
            else:
                outcome["activation"] = "already-active"

            after = asyncio.run(active_generation(args.database_url, track_id))
            if after != candidate_generation:
                raise RuntimeError(
                    f"post-activation generation {after} != {candidate_generation}"
                )
            outcome.update(
                phase="complete",
                status="activated",
                active_generation_after=after,
                completed_at=datetime.now(UTC).isoformat(),
            )
            checkpoint()
        # Checkpoint every unexpected per-track failure before stopping; losing
        # the error boundary would make a crashed rollout unauditable.
        except Exception as exc:  # noqa: BLE001
            outcome.update(
                status="failed",
                error=str(exc),
                failed_at=datetime.now(UTC).isoformat(),
            )
            payload.update(status="failed", failed_track_id=track_id)
            checkpoint()
            print(f"FAILED {track_id}: {exc}", file=sys.stderr, flush=True)
            return 1

    payload.update(status="complete", completed_at=datetime.now(UTC).isoformat())
    payload["updated_at"] = datetime.now(UTC).isoformat()
    write_checkpoint(args.output, payload)
    print(f"activated {len(tracks)} tracks; evidence -> {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

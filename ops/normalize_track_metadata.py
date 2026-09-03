#!/usr/bin/env -S uv run --script
"""Normalize ``tracks.artist`` / ``tracks.title`` across the whole library.

Reads a reviewed mapping (schema ``shizzle-track-metadata-fix-v1``) and rewrites
only the two display-metadata columns. Entries may also carry
``"action": "delete"`` to soft-delete a duplicate (same mechanism as the API
DELETE route: row lock + ``deleted_at = utcnow()``). Publication state is
untouched: no
generation, ``s3_prefix``, ``manifest_key``, or pointer changes (C-series
invariants), and no schema writes (F3) — this is a plain row update.

Safety properties:

* **Dry run by default.** Without ``--apply`` the script resolves and prints
  the full before/after plan and changes nothing.
* **All-or-nothing validation.** Every resolution problem (missing id, expect
  mismatch, ambiguous prefix, uncovered track, duplicate target) is collected
  and printed together; any violation aborts with exit 2 before any write.
* **One transaction.** ``--apply`` revalidates coverage and every row's
  before-values under row locks inside a single transaction, applies all
  updates and soft deletes, then verifies each locked row against its target
  and re-checks library coverage — any drift rolls back before commit.
* **Recoverable run record.** The report destination is reserved before any
  write; the record is written atomically after the commit, and if that
  write still fails the full record JSON is printed to stdout so it can be
  saved manually (exit 1).
* **Full coverage.** The mapping must name every non-deleted track; soft-deleted
  rows are ignored entirely.

Usage
-----
    # dry run: print the before/after table, change nothing
    uv run --directory library python ../ops/normalize_track_metadata.py

    # apply and write the run record
    uv run --directory library python ../ops/normalize_track_metadata.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_SRC = REPO_ROOT / "library" / "src"
if SERVER_SRC.is_dir():
    sys.path.insert(0, str(SERVER_SRC))

from shizzle_server.db import create_engine, create_session_factory
from shizzle_server.db.models import Track, utcnow
from sqlalchemy import select
from sqlalchemy.engine import make_url

MAPPING_SCHEMA = "shizzle-track-metadata-fix-v1"
REPORT_SCHEMA = "shizzle-track-metadata-fix-v1-run"
DEFAULT_MAPPING = REPO_ROOT / "ops" / "data" / "track-metadata-2026-09-02.json"
DEFAULT_REPORT = REPO_ROOT / "ops" / "data" / "track-metadata-2026-09-02.run.json"
PREFIX_RE = re.compile(r"[0-9a-f]{8}")


@dataclass
class PlannedChange:
    track_id: uuid.UUID
    old_artist: str
    old_title: str
    new_artist: str
    new_title: str
    note: str
    action: str = "update"  # "update" rewrites artist/title; "delete" soft-deletes

    @property
    def changed(self) -> bool:
        return self.old_artist != self.new_artist or self.old_title != self.new_title


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mapping", type=Path, default=DEFAULT_MAPPING)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the updates; absent means dry run",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help=f"JSON run record path (default on --apply: {DEFAULT_REPORT})",
    )
    return parser.parse_args(argv)


def database_host(database_url: str) -> str:
    """Return only the host portion of the DSN — never credentials."""
    try:
        return make_url(database_url).host or "(unknown)"
    except Exception:  # noqa: BLE001 - never let report metadata break the run
        return "(unknown)"


def load_mapping(path: Path) -> dict[str, Any]:
    decoded: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(decoded, dict):
        raise TypeError(f"{path}: top-level value must be an object")
    mapping: dict[str, Any] = decoded
    if mapping.get("schema") != MAPPING_SCHEMA:
        raise ValueError(
            f"{path}: schema must be {MAPPING_SCHEMA!r}, got {mapping.get('schema')!r}"
        )
    if not isinstance(mapping.get("tracks"), list):
        raise TypeError(f"{path}: 'tracks' must be a list")
    return mapping


def resolve(
    mapping: dict[str, Any], tracks: list[Track]
) -> tuple[list[PlannedChange], list[str]]:
    """Resolve every mapping entry against live rows, collecting all violations."""
    by_id = {track.id: track for track in tracks}
    violations: list[str] = []
    planned: list[PlannedChange] = []
    claimed: dict[uuid.UUID, int] = {}

    for index, entry in enumerate(mapping["tracks"]):
        if not isinstance(entry, dict):
            violations.append(f"entry[{index}]: each track entry must be an object")
            continue
        label = entry.get("id") or entry.get("expect_id_prefix") or f"entry[{index}]"
        action = entry.get("action", "update")
        if action not in {"update", "delete"}:
            violations.append(f"{label}: action must be 'update' or 'delete', got {action!r}")
            continue
        expect = entry.get("expect")
        if expect is not None and (
            not isinstance(expect, dict)
            or not isinstance(expect.get("artist"), str)
            or not isinstance(expect.get("title"), str)
        ):
            violations.append(
                f"{label}: 'expect' must be an object with artist/title strings"
            )
            continue
        if action == "update":
            for field in ("artist", "title"):
                if not isinstance(entry.get(field), str):
                    violations.append(f"{label}: missing target {field!r} string")
            if not isinstance(entry.get("artist"), str) or not isinstance(entry.get("title"), str):
                continue
        elif expect is None and not entry.get("note"):
            # A delete must carry a human-checkable anchor: either an exact
            # expect block or a note saying why the track is a duplicate.
            violations.append(f"{label}: delete entries require an 'expect' block or a 'note'")
            continue

        track: Track | None = None
        raw_id = entry.get("id")
        full_id: uuid.UUID | None = None
        if isinstance(raw_id, str):
            try:
                full_id = uuid.UUID(raw_id)
            except ValueError:
                full_id = None
            if full_id is not None:
                if entry.get("expect_id_prefix") is None and expect is None:
                    # A full id alone is not a baseline check: without expect
                    # the plan would bless whatever the row currently holds.
                    violations.append(
                        f"{label}: full-id entries require an 'expect' block"
                    )
                    continue
                track = by_id.get(full_id)
                if track is None:
                    violations.append(f"{label}: no non-deleted track with id {raw_id}")
                    continue

        prefix = entry.get("expect_id_prefix")
        if prefix is not None:
            if not (isinstance(prefix, str) and PREFIX_RE.fullmatch(prefix)):
                violations.append(f"{label}: expect_id_prefix must be 8 lowercase hex chars")
                continue
            matches = [t for t in tracks if t.id.hex.startswith(prefix)]
            if len(matches) != 1:
                violations.append(
                    f"{label}: expect_id_prefix {prefix} matches {len(matches)} "
                    f"non-deleted tracks, expected exactly 1"
                )
                continue
            prefixed = matches[0]
            if track is not None and track.id != prefixed.id:
                violations.append(
                    f"{label}: full id {track.id} and prefix {prefix} resolve to "
                    f"different tracks ({prefixed.id})"
                )
                continue
            track = prefixed

        if track is None:
            violations.append(f"{label}: entry needs a full 'id' or an 'expect_id_prefix'")
            continue
        if expect is not None and (
            track.artist != expect["artist"] or track.title != expect["title"]
        ):
            violations.append(
                f"{label}: expect mismatch — current "
                f"{track.artist!r} / {track.title!r} != expected "
                f"{expect['artist']!r} / {expect['title']!r}"
            )
            continue
        if track.id in claimed:
            violations.append(
                f"{label}: resolves to track {track.id} already targeted by "
                f"entry {claimed[track.id]}"
            )
            continue
        claimed[track.id] = index
        planned.append(
            PlannedChange(
                track_id=track.id,
                old_artist=track.artist,
                old_title=track.title,
                new_artist=track.artist if action == "delete" else entry["artist"],
                new_title=track.title if action == "delete" else entry["title"],
                note=str(entry.get("note") or ""),
                action=action,
            )
        )

    covered = set(claimed)
    for track in tracks:
        if track.id not in covered:
            violations.append(
                f"uncovered track {track.id} ({track.artist!r} / {track.title!r}) — "
                "this is a full-library normalization; every non-deleted track "
                "must be named by the mapping"
            )
    return planned, violations


def _sort_key(change: PlannedChange) -> tuple[str, str]:
    return (change.new_artist.casefold(), change.new_title.casefold())


def _print_rows(header: tuple[str, ...], body: list[tuple[str, ...]]) -> None:
    widths = [
        max(len(header[i]), *(len(row[i]) for row in body)) if body else len(header[i])
        for i in range(len(header))
    ]
    print("  ".join(header[i].ljust(widths[i]) for i in range(len(header))))
    print("  ".join("-" * widths[i] for i in range(len(header))))
    for row in body:
        print("  ".join(row[i].ljust(widths[i]) for i in range(len(header))))


def print_tables(planned: list[PlannedChange]) -> None:
    """Print updates and deletions as separate before/after tables."""
    updates = sorted((c for c in planned if c.action == "update"), key=_sort_key)
    deletions = sorted((c for c in planned if c.action == "delete"), key=_sort_key)
    if updates:
        print("updates:")
        _print_rows(
            ("id", "old artist", "old title", "new artist", "new title", "note"),
            [
                (
                    change.track_id.hex[:8],
                    change.old_artist,
                    change.old_title,
                    change.new_artist,
                    change.new_title,
                    change.note,
                )
                for change in updates
            ],
        )
    if deletions:
        print("deletions (soft delete):")
        _print_rows(
            ("id", "artist", "title", "note"),
            [
                (
                    change.track_id.hex[:8],
                    change.old_artist,
                    change.old_title,
                    change.note,
                )
                for change in deletions
            ],
        )


async def fetch_tracks(database_url: str) -> list[Track]:
    engine = create_engine(database_url)
    try:
        async with create_session_factory(engine)() as session:
            stmt = select(Track).where(Track.deleted_at.is_(None))
            result = await session.execute(stmt)
            return list(result.scalars().all())
    finally:
        await engine.dispose()


async def apply_changes(
    database_url: str, planned: list[PlannedChange]
) -> list[PlannedChange]:
    """Apply updates and soft deletes in one transaction, verifying before commit.

    Soft delete uses the same mechanism as the API DELETE route
    (TrackRepository.soft_delete): row lock, set deleted_at = utcnow().

    The validation snapshot from ``resolve`` can go stale between the dry-run
    session and this transaction, so the transaction revalidates everything
    it relies on before committing:

    * library coverage is re-enumerated at the start of the transaction and
      again just before commit (catches rows published mid-run under READ
      COMMITTED; a row committed between the final check and COMMIT itself
      is outside any non-serializable tool's reach and is caught on rerun);
    * every locked row's artist/title must still equal the validated
      before-values, else the transaction aborts and rolls back;
    * after the writes are flushed, every locked row is re-checked against
      its target state before commit.

    Any mismatch raises, the context manager rolls back, and nothing is
    committed.
    """
    planned_ids = {change.track_id for change in planned}
    update_ids = {change.track_id for change in planned if change.action == "update"}
    engine = create_engine(database_url)
    try:
        session_factory = create_session_factory(engine)
        async with session_factory() as session, session.begin():
            result = await session.execute(
                select(Track.id).where(Track.deleted_at.is_(None))
            )
            live_ids = set(result.scalars().all())
            if live_ids != planned_ids:
                missing = sorted(str(i) for i in planned_ids - live_ids)
                extra = sorted(str(i) for i in live_ids - planned_ids)
                raise RuntimeError(
                    "library changed since validation; aborting. "
                    f"planned ids no longer live: {missing}; "
                    f"newly live uncovered ids: {extra}"
                )
            for change in planned:
                track = await session.get(Track, change.track_id, with_for_update=True)
                if track is None or track.deleted_at is not None:
                    raise RuntimeError(f"track {change.track_id} vanished mid-apply; aborting")
                if track.artist != change.old_artist or track.title != change.old_title:
                    raise RuntimeError(
                        f"track {change.track_id} changed since validation "
                        f"({track.artist!r} / {track.title!r} != validated "
                        f"{change.old_artist!r} / {change.old_title!r}); aborting"
                    )
                if change.action == "delete":
                    track.deleted_at = utcnow()
                else:
                    track.artist = change.new_artist
                    track.title = change.new_title
            await session.flush()
            # Re-check every locked row against its target before committing;
            # the row locks mean no other transaction can have changed them.
            for change in planned:
                track = await session.get(Track, change.track_id)
                if track is None:
                    raise RuntimeError(f"track {change.track_id} missing after apply")
                if change.action == "delete":
                    if track.deleted_at is None:
                        raise RuntimeError(
                            f"track {change.track_id} verify mismatch: deleted_at not set"
                        )
                elif track.artist != change.new_artist or track.title != change.new_title:
                    raise RuntimeError(
                        f"track {change.track_id} verify mismatch: "
                        f"{track.artist!r} / {track.title!r}"
                    )
            # Final coverage check: any row published and committed during this
            # transaction is visible now and aborts the commit.
            result = await session.execute(
                select(Track.id).where(Track.deleted_at.is_(None))
            )
            remaining_ids = set(result.scalars().all())
            if remaining_ids != update_ids:
                extra = sorted(str(i) for i in remaining_ids - update_ids)
                raise RuntimeError(
                    "library changed during apply; aborting before commit. "
                    f"uncovered newly live ids: {extra}"
                )
    finally:
        await engine.dispose()
    return planned


def build_report(
    *, database_url: str, mapping_path: Path, planned: list[PlannedChange]
) -> dict[str, Any]:
    updates = sorted((c for c in planned if c.action == "update"), key=_sort_key)
    deletions = sorted((c for c in planned if c.action == "delete"), key=_sort_key)
    report: dict[str, Any] = {
        "schema": REPORT_SCHEMA,
        "applied_at": datetime.now(UTC).isoformat(),
        "database_host": database_host(database_url),
        "mapping": str(mapping_path),
        "mapping_sha256": hashlib.sha256(mapping_path.read_bytes()).hexdigest(),
        "counts": {
            "mapping_entries": len(planned),
            "tracks_updated": sum(1 for c in updates if c.changed),
            "tracks_unchanged": sum(1 for c in updates if not c.changed),
            "deleted": len(deletions),
        },
        "tracks": [
            {
                "id": str(change.track_id),
                "before": {"artist": change.old_artist, "title": change.old_title},
                "after": {"artist": change.new_artist, "title": change.new_title},
                "note": change.note,
            }
            for change in updates
        ],
        "deletions": [
            {
                "id": str(change.track_id),
                "before": {"artist": change.old_artist, "title": change.old_title},
                "note": change.note,
            }
            for change in deletions
        ],
    }
    # When the mapping is byte-identical to the committed repo copy, record its
    # repository-relative path so the run can be replayed from the audit record
    # even if the applied path was ephemeral (e.g. /tmp/ops on the VPS).
    if DEFAULT_MAPPING.is_file() and DEFAULT_MAPPING.read_bytes() == mapping_path.read_bytes():
        report["mapping_repo_path"] = DEFAULT_MAPPING.relative_to(REPO_ROOT).as_posix()
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.database_url:
        print("DATABASE_URL or --database-url is required", file=sys.stderr)
        return 2
    try:
        mapping = load_mapping(args.mapping)
    except (OSError, TypeError, ValueError) as exc:
        print(f"mapping error: {exc}", file=sys.stderr)
        return 2

    tracks = asyncio.run(fetch_tracks(args.database_url))
    planned, violations = resolve(mapping, tracks)
    if violations:
        print(f"{len(violations)} violation(s); aborting without changes:", file=sys.stderr)
        for violation in violations:
            print(f"  - {violation}", file=sys.stderr)
        return 2

    updates = sum(1 for c in planned if c.action == "update")
    deletions = len(planned) - updates
    print(
        f"mode: {'APPLY' if args.apply else 'DRY RUN'} — "
        f"{updates} update(s), {deletions} deletion(s)"
    )
    print_tables(planned)

    if not args.apply:
        print("dry run: no changes made (pass --apply to write)")
        return 0

    report_path = args.report or DEFAULT_REPORT
    if args.report is None and report_path.exists():
        print(
            f"refusing to overwrite {report_path}: that is the committed "
            "production run record; pass --report with an explicit path",
            file=sys.stderr,
        )
        return 2
    if report_path.is_dir():
        print(f"report error: {report_path} is a directory", file=sys.stderr)
        return 2
    # Reserve the destination before touching the database so an ordinary
    # filesystem error cannot produce an unrecorded mutation.
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        probe = report_path.parent / f".{report_path.name}.probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        print(f"report error: cannot write to {report_path.parent}: {exc}", file=sys.stderr)
        return 2

    asyncio.run(apply_changes(args.database_url, planned))
    report = build_report(
        database_url=args.database_url, mapping_path=args.mapping, planned=planned
    )
    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    try:
        tmp_path = report_path.parent / f".{report_path.name}.tmp"
        tmp_path.write_text(payload, encoding="utf-8")
        os.replace(tmp_path, report_path)
    except OSError as exc:
        # The database transaction already committed; do not lose the record.
        print(
            f"ERROR: changes were committed but the run record could not be "
            f"written to {report_path}: {exc}",
            file=sys.stderr,
        )
        print("the run record JSON follows on stdout; save it as the report", file=sys.stderr)
        print(payload)
        return 1
    print(f"applied; report -> {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

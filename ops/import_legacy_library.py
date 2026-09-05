#!/usr/bin/env python
"""Import the legacy k25 karaoke library into the Phase 3 track layout.

Reads `s3://{bucket}/karaoke/pub/{folder}/` (36 folders, ~7.45 GiB — see
`docs/legacy-manifest-v3.md`) and republishes each folder as an immutable
generation under `tracks/{track_id}/1/` **in the same bucket**, then records a
`tracks` row so the library has content on day one.

Properties this script holds to:

* **Copy, never re-upload.** Every media object moves by S3 server-side copy
  (`shizzle_server.publish.Publisher.copy_object`, which falls back to
  multipart `upload_part_copy` above the 5 GiB single-copy limit). Nothing is
  downloaded. The single object written from scratch is the translated
  `manifest.json`.
* **Read-only on `karaoke/`.** Nothing under the legacy prefix is deleted,
  overwritten, or re-tagged. `GET`/`HEAD`/`LIST` only.
* **Manifest last.** Same rule as the publisher: the manifest is the completion
  marker for a generation, so it is the final `PUT`.
* **Idempotent.** Track ids come from `track_id_for_import(folder)`, so a
  re-run converges on the same prefix and the same row. A folder whose
  destination manifest already exists is skipped (its generation is immutable);
  the row is still upserted so a run interrupted between S3 and the DB heals.

Usage
-----
    # inspect without touching anything
    uv run --directory library python ../ops/import_legacy_library.py --dry-run

    # import everything
    python ops/import_legacy_library.py --database-url postgresql+asyncpg://...

    # try two first
    python ops/import_legacy_library.py --limit 2

Credentials come from `.env` (or the ambient environment with `--no-env-file`).
`AWS_ENDPOINT_URL` is force-cleared: this machine has a global R2 override that
would otherwise silently point every call at the wrong provider.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "library" / "src"))

import boto3  # noqa: E402

from shizzle_server.db import create_engine, create_session_factory  # noqa: E402
from shizzle_server.db.repository import (  # noqa: E402
    ImportConflict,
    TrackRepository,
    track_id_for_import,
)
from shizzle_server.publish import (  # noqa: E402
    MANIFEST_NAME,
    Publisher,
    generation_prefix,
    manifest_key,
    validate_stem_object,
)

logger = logging.getLogger("import_legacy")

DEFAULT_BUCKET = "karaoke-pimpshizzle"
DEFAULT_SOURCE_PREFIX = "karaoke/pub/"
LEGACY_MANIFEST_NAME = "stems.json"
GENERATION = 1

#: What the importer stamps on tracks it could not measure.
LEGACY_INTEGRITY = {"source": "legacy-import", "gates": "not-run"}

#: Legacy objects mostly carry correct Content-Types, but not all of them
#: (`the-pot-2d88b7a5/video.mp4` is stored as binary/octet-stream). The browser
#: player and CloudFront both care, so the importer restamps on copy.
CONTENT_TYPES = {
    ".mp4": "video/mp4",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
    ".json": "application/json",
}

STEM_DISPLAY_NAMES = {
    "vocals": "Vocals",
    "drums": "Drums",
    "bass": "Bass",
    "guitar": "Guitar",
    "piano": "Piano",
    "shizzle": "Shizzle",
}


# --- env ----------------------------------------------------------------------


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE lines into os.environ without clobbering what is set.

    `.env` on this machine is not pure ASCII, hence the explicit decode.
    """
    if not path.exists():
        logger.warning("no .env at %s; relying on the ambient environment", path)
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ[key] = value


def s3_client(region: str) -> Any:
    # Machine-level R2 override must never reach these AWS calls.
    os.environ.pop("AWS_ENDPOINT_URL", None)
    os.environ.pop("AWS_ENDPOINT_URL_S3", None)
    return boto3.client("s3", region_name=region)


# --- inventory ----------------------------------------------------------------


@dataclass
class LegacyFolder:
    name: str
    objects: dict[str, int]  # relative key -> size
    manifest: dict[str, Any]

    @property
    def referenced_stems(self) -> list[str]:
        return [str(s.get("file", "")) for s in self.manifest.get("stems", [])]

    @property
    def missing(self) -> list[str]:
        """Files the manifest references that are not in the bucket."""
        refs = [*self.referenced_stems, str(self.manifest.get("video", ""))]
        merged = self.manifest.get("merged_audio")
        if merged:
            refs.append(str(merged))
        return [r for r in refs if r and r not in self.objects]

    @property
    def degraded(self) -> bool:
        return bool(self.missing)

    @property
    def total_bytes(self) -> int:
        return sum(size for rel, size in self.objects.items() if rel != LEGACY_MANIFEST_NAME)


def inventory(s3: Any, bucket: str, prefix: str) -> list[LegacyFolder]:
    """List every legacy folder and read its manifest. Read-only."""
    by_folder: dict[str, dict[str, int]] = {}
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            rest = obj["Key"][len(prefix) :]
            if "/" not in rest:
                continue
            folder, rel = rest.split("/", 1)
            if not rel:
                continue
            by_folder.setdefault(folder, {})[rel] = obj["Size"]

    folders: list[LegacyFolder] = []
    for name in sorted(by_folder):
        objects = by_folder[name]
        if LEGACY_MANIFEST_NAME not in objects:
            logger.warning("skipping %s: no %s", name, LEGACY_MANIFEST_NAME)
            continue
        body = s3.get_object(Bucket=bucket, Key=f"{prefix}{name}/{LEGACY_MANIFEST_NAME}")["Body"]
        folders.append(LegacyFolder(name=name, objects=objects, manifest=json.loads(body.read())))
    return folders


# --- translation --------------------------------------------------------------


def translate_manifest(folder: LegacyFolder) -> dict[str, Any]:
    """Legacy `stems.json` -> current v3 manifest. See docs/legacy-manifest-v3.md."""
    legacy = folder.manifest
    duration = float(legacy.get("duration", 0.0) or 0.0)

    stems: list[dict[str, Any]] = []
    for entry in legacy.get("stems", []):
        stem_id = str(entry.get("id", ""))
        file = str(entry.get("file", ""))
        if file not in folder.objects:
            # Degraded folder: drop the stem rather than emit a dangling ref.
            continue
        translated: dict[str, Any] = {
            "id": stem_id,
            "name": STEM_DISPLAY_NAMES.get(stem_id, entry.get("name") or stem_id.title()),
            "file": file,
            # The unit-bearing field. Legacy `default_gain` was 0 (linear =
            # silence) on every v3 folder and 1.0 on the v2-era one; both mean
            # "play at unity" in practice. 0.0 dB says that unambiguously.
            "default_gain_db": 0.0,
        }
        # Legacy `channel_offset` (the channel-pair index into multi-track.mp4)
        # is deliberately dropped: it is not in the current schema, and
        # TypeScript's excess-property check rejects it on a `Stem` literal.
        # The mapping survives in the untouched legacy `stems.json`.
        stems.append(translated)

    manifest: dict[str, Any] = {
        "version": 3,
        "title": legacy.get("title") or folder.name,
        "artist": legacy.get("artist", "") or "",
        "duration": duration,
        "video": legacy.get("video", "video.mp4"),
        "sourceUrl": legacy.get("sourceUrl", "") or "",
        "videoId": legacy.get("videoId", "") or "",
        "video_meta": legacy.get("video_meta") or {},
        "stems": stems,
        "integrity": dict(LEGACY_INTEGRITY),
        "processing": {"source": "legacy-import", "origin": f"karaoke/pub/{folder.name}"},
    }

    timeline = legacy.get("timeline")
    if isinstance(timeline, dict):
        # Carried verbatim, including sample_rate_hz=48000 — the importer does
        # not decode the stems, so it cannot verify or correct that number.
        manifest["timeline"] = timeline

    if "multi-track.mp4" in folder.objects:
        # Legacy manifests never referenced the mux even when it existed.
        manifest["multitrack"] = "multi-track.mp4"
    merged = legacy.get("merged_audio")
    if merged and merged in folder.objects:
        manifest["merged_audio"] = merged

    return manifest


# --- import -------------------------------------------------------------------


@dataclass
class ImportOutcome:
    folder: str
    track_id: uuid.UUID
    title: str
    status: str  # imported | already-published | manifest-rewritten | skipped-degraded | skipped-invalid-stems | skipped-existing-advanced | skipped-deleted | dry-run | failed
    objects_copied: int = 0
    bytes_copied: int = 0
    elapsed_s: float = 0.0
    row_created: bool | None = None
    reason: str | None = None
    manifest: dict[str, Any] = field(default_factory=dict)


def copy_folder(
    publisher: Publisher,
    source_prefix: str,
    folder: LegacyFolder,
    track_id: uuid.UUID,
    manifest: dict[str, Any],
) -> tuple[int, int]:
    """Server-side copy every media object, then PUT the manifest LAST."""
    src = f"{source_prefix}{folder.name}/"
    dst = generation_prefix(track_id, GENERATION)

    copied = 0
    total = 0
    # Deterministic order so interrupted runs and logs line up.
    for rel in sorted(folder.objects):
        if rel == LEGACY_MANIFEST_NAME:
            continue  # replaced by the translated manifest, written last
        size = folder.objects[rel]
        suffix = Path(rel).suffix.lower()
        publisher.copy_object(
            f"{src}{rel}", f"{dst}{rel}", size, content_type=CONTENT_TYPES.get(suffix)
        )
        copied += 1
        total += size

    publisher.s3.put_object(
        Bucket=publisher.bucket,
        Key=manifest_key(track_id, GENERATION),
        Body=json.dumps(manifest, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    return copied + 1, total


def verify_destination(
    publisher: Publisher, folder: LegacyFolder, track_id: uuid.UUID
) -> list[str]:
    """Re-list the destination and confirm sizes match. Returns problems."""
    dst = generation_prefix(track_id, GENERATION)
    landed = publisher.list_prefix(dst)
    problems = []
    for rel, size in folder.objects.items():
        if rel == LEGACY_MANIFEST_NAME:
            continue
        if rel not in landed:
            problems.append(f"missing at destination: {rel}")
        elif landed[rel] != size:
            problems.append(f"size mismatch {rel}: source {size}, dest {landed[rel]}")
    if MANIFEST_NAME not in landed:
        problems.append("manifest.json not written")
    return problems


async def run(args: argparse.Namespace) -> list[ImportOutcome]:
    s3 = s3_client(args.region)
    publisher = Publisher(s3, args.bucket)

    logger.info("inventorying s3://%s/%s", args.bucket, args.source_prefix)
    folders = inventory(s3, args.bucket, args.source_prefix)
    logger.info(
        "found %d folders, %d objects, %.2f GiB",
        len(folders),
        sum(len(f.objects) for f in folders),
        sum(f.total_bytes for f in folders) / 1024**3,
    )

    if args.only:
        wanted = set(args.only)
        folders = [f for f in folders if f.name in wanted]
    if args.limit:
        folders = folders[: args.limit]

    session_factory = None
    engine = None
    if not args.dry_run:
        engine = create_engine(args.database_url)
        session_factory = create_session_factory(engine)

    outcomes: list[ImportOutcome] = []
    try:
        for index, folder in enumerate(folders, 1):
            outcome = await import_folder(
                publisher, folder, args, session_factory, index, len(folders)
            )
            outcomes.append(outcome)
    finally:
        if engine is not None:
            await engine.dispose()
    return outcomes


async def import_folder(
    publisher: Publisher,
    folder: LegacyFolder,
    args: argparse.Namespace,
    session_factory: Any,
    index: int,
    total: int,
) -> ImportOutcome:
    track_id = track_id_for_import(folder.name)
    manifest = translate_manifest(folder)
    title = str(manifest["title"])
    outcome = ImportOutcome(
        folder=folder.name, track_id=track_id, title=title, status="failed", manifest=manifest
    )

    label = f"[{index}/{total}] {folder.name} {title[:48]!r}"

    # Stem format guard (shared with the publisher): raw WAV or oversized
    # stems must never enter the library — "The Pot" (2026-08-04) was imported
    # with 6 × 95 MiB WAVs and froze the player. No bypass flag on purpose;
    # transcode the source to .m4a instead.
    invalid = [
        f"{stem['file']}: {reason}"
        for stem in manifest["stems"]
        if (reason := validate_stem_object(stem["file"], folder.objects.get(stem["file"])))
        is not None
    ]
    if invalid:
        outcome.status = "skipped-invalid-stems"
        outcome.reason = "; ".join(invalid[:4])
        logger.error("%s SKIPPED (invalid stems): %s", label, outcome.reason)
        return outcome

    if folder.degraded and not args.include_degraded:
        outcome.status = "skipped-degraded"
        outcome.reason = f"manifest references {len(folder.missing)} absent objects"
        logger.warning("%s SKIPPED (degraded): %s", label, ", ".join(folder.missing[:3]) + " …")
        return outcome

    if args.dry_run:
        outcome.status = "dry-run"
        outcome.objects_copied = len(folder.objects)  # media + new manifest
        outcome.bytes_copied = folder.total_bytes
        logger.info(
            "%s DRY-RUN -> %s (%d objects, %.1f MiB, %d stems)",
            label,
            generation_prefix(track_id, GENERATION),
            outcome.objects_copied,
            outcome.bytes_copied / 1024**2,
            len(manifest["stems"]),
        )
        return outcome

    started = time.monotonic()
    # Issue #33, first line of defense: check the row BEFORE any S3 write, so
    # a skipped rerun cannot mutate generation-1 storage (manifest rewrite or
    # object copy). The repository guard below is the second line.
    if session_factory is not None:
        repo = TrackRepository(session_factory)
        existing = await repo.get(track_id)
        if existing is not None and existing.generation != GENERATION:
            outcome.status = "skipped-existing-advanced"
            outcome.reason = (
                f"row at generation {existing.generation}, importer pins {GENERATION}"
            )
            outcome.elapsed_s = round(time.monotonic() - started, 2)
            logger.warning("%s SKIPPED: %s", label, outcome.reason)
            return outcome

    already = publisher.is_published(track_id, GENERATION)
    try:
        if already and args.rewrite_manifests:
            # Deliberate, opt-in exception to generation immutability: correct
            # the manifest of a generation that nothing has served yet. Safe
            # only before the CDN is in front of the bucket (Phase 4) — after
            # that, cut a new generation instead.
            publisher.s3.put_object(
                Bucket=publisher.bucket,
                Key=manifest_key(track_id, GENERATION),
                Body=json.dumps(manifest, indent=2).encode("utf-8"),
                ContentType="application/json",
            )
            outcome.status = "manifest-rewritten"
            outcome.objects_copied = 1
            logger.warning("%s manifest REWRITTEN in place (media untouched)", label)
        elif already:
            outcome.status = "already-published"
            logger.info("%s already published, S3 untouched", label)
        else:
            copied, copied_bytes = copy_folder(
                publisher, args.source_prefix, folder, track_id, manifest
            )
            outcome.objects_copied = copied
            outcome.bytes_copied = copied_bytes
            outcome.status = "imported"

        problems = verify_destination(publisher, folder, track_id)
        if problems:
            outcome.status = "failed"
            outcome.reason = "; ".join(problems[:4])
            logger.error("%s VERIFY FAILED: %s", label, outcome.reason)
            return outcome

        repo = TrackRepository(session_factory)
        try:
            _, created = await repo.upsert_imported(
                track_id,
                title=title,
                artist=str(manifest["artist"]),
                duration_seconds=float(manifest["duration"]),
                s3_prefix=generation_prefix(track_id, GENERATION).rstrip("/"),
                manifest_key=manifest_key(track_id, GENERATION),
                generation=GENERATION,
                integrity={**LEGACY_INTEGRITY, "legacy_folder": folder.name},
            )
        except ImportConflict as exc:
            # e.g. a soft-deleted duplicate: the decision stands, do not
            # resurrect it and do not fail the whole run (issue #33).
            outcome.status = "skipped-deleted" if exc.deleted else "skipped-existing-advanced"
            outcome.reason = f"ImportConflict: {exc}"
            outcome.elapsed_s = round(time.monotonic() - started, 2)
            logger.warning("%s SKIPPED: %s", label, outcome.reason)
            return outcome
        outcome.row_created = created
        outcome.elapsed_s = round(time.monotonic() - started, 2)
        logger.info(
            "%s %s: %d objects, %.1f MiB in %.1fs (row %s)",
            label,
            outcome.status,
            outcome.objects_copied,
            outcome.bytes_copied / 1024**2,
            outcome.elapsed_s,
            "created" if created else "updated",
        )
    except Exception as exc:  # one bad folder must not stop the library import
        outcome.status = "failed"
        outcome.reason = f"{type(exc).__name__}: {exc}"[:300]
        outcome.elapsed_s = round(time.monotonic() - started, 2)
        logger.exception("%s FAILED", label)
    return outcome


# --- cli ----------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--source-prefix", default=DEFAULT_SOURCE_PREFIX)
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument(
        "--database-url",
        default=None,
        help="async SQLAlchemy DSN; defaults to $DATABASE_URL from .env",
    )
    parser.add_argument("--dry-run", action="store_true", help="inventory and translate only")
    parser.add_argument("--limit", type=int, default=0, help="import at most N folders")
    parser.add_argument("--only", action="append", default=[], help="import just this folder")
    parser.add_argument(
        "--include-degraded",
        action="store_true",
        help="also import folders whose manifest references objects that do not exist "
        "(their stems[] is emitted with only the files that survive)",
    )
    parser.add_argument(
        "--rewrite-manifests",
        action="store_true",
        help="re-translate and overwrite manifest.json for already-published generations, "
        "leaving media untouched. Opt-in exception to generation immutability — only safe "
        "before a CDN is serving the prefix.",
    )
    parser.add_argument("--no-env-file", action="store_true")
    parser.add_argument("--report", type=Path, default=None, help="write a JSON run report here")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    if not args.no_env_file:
        load_env_file(REPO_ROOT / ".env")
    if args.database_url is None:
        args.database_url = os.environ.get("DATABASE_URL", "")
    if not args.dry_run and not args.database_url:
        logger.error("no --database-url and no DATABASE_URL in the environment")
        return 2

    started = time.monotonic()
    outcomes = asyncio.run(run(args))
    elapsed = time.monotonic() - started

    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    total_bytes = sum(o.bytes_copied for o in outcomes)

    print("\n=== import summary ===")
    for status, n in sorted(counts.items()):
        print(f"  {status:20s} {n}")
    print(f"  {'bytes copied':20s} {total_bytes:,} ({total_bytes / 1024**3:.2f} GiB)")
    print(f"  {'elapsed':20s} {elapsed:.1f}s")
    failures = [o for o in outcomes if o.status == "failed"]
    for o in failures:
        print(f"  FAILED {o.folder}: {o.reason}")

    if args.report:
        args.report.write_text(
            json.dumps(
                {
                    "elapsed_s": round(elapsed, 2),
                    "counts": counts,
                    "bytes_copied": total_bytes,
                    "outcomes": [
                        {
                            "folder": o.folder,
                            "track_id": str(o.track_id),
                            "title": o.title,
                            "status": o.status,
                            "objects_copied": o.objects_copied,
                            "bytes_copied": o.bytes_copied,
                            "elapsed_s": o.elapsed_s,
                            "row_created": o.row_created,
                            "reason": o.reason,
                            "stem_count": len(o.manifest.get("stems", [])),
                            "duration": o.manifest.get("duration"),
                        }
                        for o in outcomes
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"  report -> {args.report}")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

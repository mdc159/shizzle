"""VPS-side drop-box ingest for completed ``shizzle-browser-v1`` packages.

A producer (e.g. the Mac pipeline) finishes a package — six AAC stems, one
silent video, one v3 manifest — already in the published browser layout and
drops it into S3 under

    imports/{source_ref}/
        stems/{vocals,drums,bass,guitar,piano,shizzle}.m4a
        video.mp4
        manifest.json      <- uploaded LAST; presence = drop complete (A1/C2)
        result.json        <- written by this module only; ignored on read

This module is the ingest half of that drop-box (issue #45). It validates the
drop with the EXISTING publisher/audit code, publishes it immutably, and
registers the track — with no re-separation, no source download and no
re-encode. The media bytes are copied by S3 server-side copy only (C2) after
their sha256s are re-proven against the downloaded objects (A4) and the full
delivery-profile audit passes with ``preserve_existing_lossy=True`` (D5: the
VPS never re-encodes dropped bytes, so the spec's "existing audio" row applies
and sparse-stem ``audio-bitrate-low`` is a recorded warning, not a rejection —
issue #22). A drop is registered only after every gate passes (C8); a rejected
drop leaves no row, no generation manifest, and every dropped object in place.

Identity is the deterministic import id ``track_id_for_import(source_ref)``
(C4), so a crashed-and-rerun ingest converges: staging copies are idempotent,
``publish`` no-ops on an existing generation manifest (C1), and
``upsert_imported`` takes the row ``FOR UPDATE`` (C5). An existing generation
is treated as a completed retry only when its published manifest hash equals
the dropped manifest's hash; anything else is a ``TRACK_CONFLICT``. The
identity check deliberately runs BEFORE the inventory check: after a
successful ingest the dropped media are deleted, so a retry must be
recognised by the published manifest hash before any inventory check. Two
race fences back that up for concurrent ingests of different content: the
published manifest hash is re-read after ``publish`` (which no-ops on an
existing generation) and the row's recorded ``manifest_sha256`` is re-checked
immediately before registration — a residual window remains between that
read and the ``FOR UPDATE`` write; the drop-box is single-operator today.

Usage (the api image has ffmpeg/ffprobe and the same ``.env`` as the
orchestrator):

    docker compose -f deploy/vps/compose.prod.yml exec api \
        python -m shizzle_server.publish.browser_import --source-ref <ref>

Exit codes: 0 published / already-published / would-publish, 2 rejected,
3 not ready. Credentials never appear in output (E3).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..db.repository import ImportConflict, TrackRepository, track_id_for_import
from ..metadata import resolve_track_metadata
from .delivery_profile import (
    AUDIO_SAMPLE_RATE,
    CANONICAL_STEM_IDS,
    MAX_TOTAL_AVERAGE_BITRATE,
    PROFILE_ID,
    STEM_INTER_DURATION_TOLERANCE_SEC,
    ProfileIssue,
    evaluate_manifest,
    has_errors,
)
from .lossless_intake import _download_object, _head_or_none
from .media_audit import MediaAuditError, audit_audio_file, audit_video_file, sha256_file
from .publisher import (
    MAX_STEM_BYTES,
    Publisher,
    PublishError,
    StagedObject,
    _is_not_found,
    generation_prefix,
    manifest_key,
    staging_prefix,
    validate_stem_objects,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..db.models import Track
    from .publisher import PublishResult

logger = logging.getLogger("browser_import")

#: Drop-box namespace. The ``youtube-``/``sha256-`` prefix keeps these ids
#: disjoint from the legacy importer's ``karaoke/pub/...`` refs; anything else
#: is refused before any S3 call is made.
SOURCE_REF_RE = re.compile(r"^(youtube|sha256)-[A-Za-z0-9_-]{6,128}$")
IMPORTS_PREFIX = "imports/"
#: Same browser-video staging ceiling the player enforces on its Blob.
MAX_IMPORT_VIDEO_BYTES = 128 * 1024**2
RESULT_NAME = "result.json"
#: Headroom required on the scratch volume next to the media bytes.
_DISK_HEADROOM_BYTES = 256 * 1024**2


class ImportRejected(RuntimeError):
    """A terminal rejection; ``code`` names the gate, ``issues`` the evidence."""

    def __init__(
        self,
        code: str,
        issues: list[dict[str, Any]],
        *,
        generation: int = 1,
        result: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(code, issues)
        self.code = code
        self.issues = issues
        self.generation = generation
        self.result = result


class ImportNotReady(RuntimeError):
    """The drop has no manifest.json yet; nothing is written."""


def _issue(code: str, message: str, artifact: str | None = None) -> dict[str, Any]:
    return ProfileIssue(code, message, artifact).as_dict()


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


# --- manifest shape (pure) ----------------------------------------------------


def validate_import_manifest(
    manifest: dict[str, Any], *, max_duration_seconds: float
) -> list[ProfileIssue]:
    """Shape-check a dropped v3 manifest on top of ``evaluate_manifest``.

    The profile evaluator covers roles, canonical stem paths, stem count and
    ``video == "video.mp4"``; this adds the drop-box-specific rules: v3
    version, profile id, canonical stem ORDER, one common finite attenuation,
    bounded duration, the exact timeline block, a non-empty title, and an
    ``integrity.objects`` list declaring exactly the seven media files with
    positive sizes and lowercase sha256 digests under the C3 caps.
    """
    issues = list(evaluate_manifest(manifest))

    if manifest.get("version") != 3:
        issues.append(
            ProfileIssue("manifest-version", f"version must be 3, got {manifest.get('version')!r}")
        )
    if manifest.get("delivery_profile") != PROFILE_ID:
        issues.append(
            ProfileIssue(
                "manifest-profile",
                f"delivery_profile must be {PROFILE_ID}, got {manifest.get('delivery_profile')!r}",
            )
        )

    stems = manifest.get("stems")
    if isinstance(stems, list) and all(isinstance(s, dict) for s in stems):
        ids = [str(s.get("id", "")) for s in stems]
        if ids != list(CANONICAL_STEM_IDS):
            issues.append(
                ProfileIssue(
                    "manifest-stem-order",
                    "stems must appear in canonical order: " + ", ".join(CANONICAL_STEM_IDS),
                    "stems",
                )
            )
        gains = [_finite_number(s.get("default_gain_db")) for s in stems]
        for stem, gain in zip(stems, gains, strict=True):
            role = str(stem.get("id", ""))
            if gain is None:
                issues.append(
                    ProfileIssue(
                        "manifest-gain-invalid",
                        "default_gain_db must be a finite number",
                        role,
                    )
                )
            elif gain > 0:
                issues.append(
                    ProfileIssue(
                        "manifest-gain-positive",
                        "default_gain_db must be an attenuation (<= 0 dB), never a boost",
                        role,
                    )
                )
        present = [g for g in gains if g is not None]
        if stems and len(present) == len(gains) and len(set(present)) > 1:
            issues.append(
                ProfileIssue(
                    "manifest-gain-not-common",
                    "all stems must share ONE common default_gain_db (D3)",
                    "stems",
                )
            )

    duration = _finite_number(manifest.get("duration"))
    if duration is None or not 0 < duration <= max_duration_seconds:
        issues.append(
            ProfileIssue(
                "manifest-duration",
                f"duration must be finite with 0 < duration <= {max_duration_seconds}s, "
                f"got {manifest.get('duration')!r}",
            )
        )
        duration = None

    timeline = manifest.get("timeline")
    _timeline_keys = {"start_ms", "duration_ms", "sample_rate_hz"}
    if not isinstance(timeline, dict):
        issues.append(ProfileIssue("manifest-timeline", "timeline must be an object", "timeline"))
    elif set(timeline) != _timeline_keys:
        detail = []
        if extra := sorted(set(timeline) - _timeline_keys):
            detail.append(f"unexpected keys: {', '.join(str(k) for k in extra)}")
        if missing := sorted(_timeline_keys - set(timeline)):
            detail.append(f"missing keys: {', '.join(str(k) for k in missing)}")
        issues.append(
            ProfileIssue(
                "manifest-timeline",
                "timeline must have exactly the keys start_ms, duration_ms, sample_rate_hz"
                + (f" ({'; '.join(detail)})" if detail else ""),
                "timeline",
            )
        )
    elif timeline.get("start_ms") != 0 or timeline.get("sample_rate_hz") != AUDIO_SAMPLE_RATE:
        issues.append(
            ProfileIssue(
                "manifest-timeline",
                "timeline must declare start_ms 0 and sample_rate_hz 44100",
                "timeline",
            )
        )
    elif duration is not None:
        expected_ms = round(duration * 1000)
        actual_ms = timeline.get("duration_ms")
        if (
            isinstance(actual_ms, bool)
            or not isinstance(actual_ms, int | float)
            or abs(actual_ms - expected_ms) > 1
        ):
            issues.append(
                ProfileIssue(
                    "manifest-timeline",
                    f"timeline.duration_ms must be {expected_ms} (±1 ms), got {actual_ms!r}",
                    "timeline",
                )
            )

    title = manifest.get("title")
    if not isinstance(title, str) or not title.strip():
        issues.append(ProfileIssue("manifest-title", "title must be a non-empty string"))

    integrity = manifest.get("integrity")
    objects = integrity.get("objects") if isinstance(integrity, dict) else None
    if not isinstance(objects, list):
        issues.append(
            ProfileIssue(
                "manifest-objects-missing",
                "integrity.objects must be a list declaring the seven media files",
            )
        )
        objects = []
    declared: dict[str, tuple[int, str]] = {}
    for entry in objects:
        if not isinstance(entry, dict):
            issues.append(
                ProfileIssue("manifest-object-invalid", f"entry must be an object: {entry!r}")
            )
            continue
        file = entry.get("file")
        if not isinstance(file, str):
            issues.append(ProfileIssue("manifest-object-invalid", "entry must declare a file"))
            continue
        if file in declared:
            # A silent dict overwrite would hide a second, possibly different
            # declaration of the same file behind the first one.
            issues.append(
                ProfileIssue(
                    "manifest-object-duplicate",
                    f"{file}: declared more than once in integrity.objects",
                    file,
                )
            )
            continue
        size = entry.get("bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size < 1:
            issues.append(
                ProfileIssue(
                    "manifest-object-bytes", f"{file}: bytes must be a positive integer", file
                )
            )
            continue
        sha = entry.get("sha256")
        if not isinstance(sha, str) or re.fullmatch(r"[0-9a-f]{64}", sha) is None:
            issues.append(
                ProfileIssue(
                    "manifest-object-sha",
                    f"{file}: sha256 must be a lowercase 64-char hex digest",
                    file,
                )
            )
            continue
        declared[file] = (size, sha)

    wanted = {"video.mp4"} | {f"stems/{role}.m4a" for role in CANONICAL_STEM_IDS}
    for file in sorted(wanted - set(declared)):
        issues.append(
            ProfileIssue("manifest-objects-missing", f"integrity.objects does not declare {file}", file)
        )
    for file in sorted(set(declared) - wanted):
        issues.append(
            ProfileIssue("manifest-objects-extra", f"integrity.objects declares unexpected {file}", file)
        )
    for file, (size, _sha) in sorted(declared.items()):
        if file.startswith("stems/") and size > MAX_STEM_BYTES:
            issues.append(
                ProfileIssue(
                    "manifest-object-too-large",
                    f"{file}: {size} bytes exceeds the {MAX_STEM_BYTES} byte stem cap (C3)",
                    file,
                )
            )
        elif file == "video.mp4" and size > MAX_IMPORT_VIDEO_BYTES:
            issues.append(
                ProfileIssue(
                    "manifest-object-too-large",
                    f"{file}: {size} bytes exceeds the {MAX_IMPORT_VIDEO_BYTES} byte video cap",
                    file,
                )
            )
    return issues


def expected_media_files(manifest: dict[str, Any]) -> dict[str, tuple[int, str]]:
    """``{file: (bytes, sha256)}`` for the seven declared media files."""
    objects = manifest.get("integrity", {}).get("objects", [])
    out: dict[str, tuple[int, str]] = {}
    for entry in objects:
        if isinstance(entry, dict) and isinstance(entry.get("file"), str):
            out[entry["file"]] = (int(entry["bytes"]), str(entry["sha256"]))
    return out


def inventory_problems(
    listed: dict[str, int], expected: dict[str, tuple[int, str]]
) -> list[str]:
    """Compare an S3 listing of the drop against the manifest's declaration.

    ``listed`` is ``Publisher.list_prefix`` output (relative key -> size).
    ``result.json`` (ingest-owned) and ``manifest.json`` (the completion
    marker, A1/C2) are expected residents, not extras. Missing files, extra
    files and size mismatches are problems.
    """
    problems: list[str] = []
    live = {k: v for k, v in listed.items() if k not in (RESULT_NAME, "manifest.json")}
    for file, (size, _sha) in sorted(expected.items()):
        if file not in live:
            problems.append(f"missing from drop: {file}")
        elif live[file] != size:
            problems.append(f"size mismatch {file}: listed {live[file]}, declared {size}")
    for file in sorted(set(live) - set(expected)):
        problems.append(f"unexpected object under the drop prefix: {file}")
    return problems


# --- downloaded-byte gates (A4 + C8) ------------------------------------------


def _measured_duration(audit: dict[str, Any]) -> float | None:
    probe = audit.get("probe") or {}
    value = (probe.get("format") or {}).get("duration")
    if value is None:
        for stream in probe.get("streams", []):
            if stream.get("codec_type") == "audio" and stream.get("duration") is not None:
                value = stream["duration"]
                break
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def validate_downloaded_bytes(
    workdir: Path,
    manifest: dict[str, Any],
    expected: dict[str, tuple[int, str]],
) -> list[dict[str, Any]]:
    """Re-prove the dropped bytes locally, then run the full delivery audit.

    Every downloaded object's sha256 must equal the declared one (A4: the
    manifest is never trusted). Each stem then goes through
    ``audit_audio_file`` and the video through ``audit_video_file`` — the same
    auditors the lossless intake uses. On top of the auditors this adds the
    checks they cannot make: inter-stem duration spread, the C3 stem-format
    guard over the actual sizes, and the complete-generation average-bitrate
    budget. Any error-severity issue raises ``ImportRejected`` (C8); the
    returned audit list still carries warnings.
    """
    issues: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []
    duration = float(manifest["duration"])

    for role in CANONICAL_STEM_IDS:
        rel = f"stems/{role}.m4a"
        path = workdir / "stems" / f"{role}.m4a"
        _size, declared_sha = expected[rel]
        if sha256_file(path) != declared_sha:
            issues.append(
                _issue(
                    "sha256-mismatch",
                    f"{rel}: downloaded bytes do not match the declared sha256",
                    rel,
                )
            )
        try:
            # preserve_existing_lossy=True is REQUIRED here: the VPS never
            # re-encodes dropped bytes, so the profile's "existing audio" row
            # applies and a sparse stem's audio-bitrate-low stays a recorded
            # WARNING instead of a rejection (issue #22).
            audits.append(
                audit_audio_file(
                    path,
                    artifact=rel,
                    expected_duration=duration,
                    preserve_existing_lossy=True,
                )
            )
        except (OSError, MediaAuditError) as exc:
            issues.append(_issue("audit-failed", f"{rel}: {exc}", rel))

    video = workdir / "video.mp4"
    _size, video_sha = expected["video.mp4"]
    if sha256_file(video) != video_sha:
        issues.append(
            _issue(
                "sha256-mismatch",
                "video.mp4: downloaded bytes do not match the declared sha256",
                "video.mp4",
            )
        )
    try:
        # The video audit flags an audio stream in the delivery video as
        # `video-has-audio` at error severity (D6) — an error here rejects.
        audits.append(
            audit_video_file(video, artifact="video.mp4", expected_duration=duration)
        )
    except (OSError, MediaAuditError) as exc:
        issues.append(_issue("audit-failed", f"video.mp4: {exc}", "video.mp4"))

    for audit in audits:
        issues.extend(i for i in audit.get("issues", []) if i.get("severity") == "error")

    stem_durations = [
        d
        for d in (
            _measured_duration(a) for a in audits if str(a.get("artifact", "")).startswith("stems/")
        )
        if d is not None
    ]
    if len(stem_durations) == len(CANONICAL_STEM_IDS):
        spread = max(stem_durations) - min(stem_durations)
        if spread > STEM_INTER_DURATION_TOLERANCE_SEC:
            issues.append(
                _issue(
                    "stem-inter-duration-spread",
                    f"measured stem durations spread {spread:.3f}s, over the "
                    f"{STEM_INTER_DURATION_TOLERANCE_SEC:.3f}s identical-timeline bound (D2)",
                    "stems",
                )
            )

    try:
        validate_stem_objects((file, (workdir / file).stat().st_size) for file in sorted(expected))
    except PublishError as exc:  # InvalidStemObject — the C3 format guard
        issues.append(_issue("stem-format", str(exc), "stems"))

    total_bytes = sum((workdir / file).stat().st_size for file in expected)
    average_bps = total_bytes * 8 / duration
    if average_bps > MAX_TOTAL_AVERAGE_BITRATE:
        issues.append(
            _issue(
                "total-average-bitrate",
                f"complete generation averages {average_bps / 1e6:.3f} Mb/s, over the "
                f"{MAX_TOTAL_AVERAGE_BITRATE / 1e6:.1f} Mb/s budget (D4)",
            )
        )

    if issues:
        raise ImportRejected("INTEGRITY_GATE_FAILED", issues)
    return audits


def manifest_sha256(raw_bytes: bytes) -> str:
    return hashlib.sha256(raw_bytes).hexdigest()


# --- DB helpers (async only here, mirroring lossless_intake.activate) ---------


def _get_track(database_url: str, track_id: uuid.UUID) -> Track | None:
    async def _run() -> Track | None:
        from ..db import create_engine, create_session_factory

        engine = create_engine(database_url)
        try:
            return await TrackRepository(create_session_factory(engine)).get(track_id)
        finally:
            await engine.dispose()

    return asyncio.run(_run())


def _register_track(
    database_url: str,
    track_id: uuid.UUID,
    generation: int,
    *,
    source_ref: str,
    manifest: dict[str, Any],
    manifest_sha: str,
    audits: list[dict[str, Any]],
    publish_result: PublishResult | None,
) -> None:
    meta = resolve_track_metadata(manifest.get("title"), manifest.get("artist"), None)
    integrity: dict[str, Any] = {
        "source": "browser-import",
        "source_ref": source_ref,
        "manifest_sha256": manifest_sha,
        "manifest": manifest.get("integrity"),
        "audit": audits,
    }
    if publish_result is not None and publish_result.verification is not None:
        integrity["publisher"] = publish_result.verification.to_integrity()

    async def _run() -> None:
        from ..db import create_engine, create_session_factory

        engine = create_engine(database_url)
        try:
            await TrackRepository(create_session_factory(engine)).upsert_imported(
                track_id,
                title=meta.title,
                artist=meta.artist,
                duration_seconds=float(manifest["duration"]),
                s3_prefix=generation_prefix(track_id, generation).rstrip("/"),
                manifest_key=manifest_key(track_id, generation),
                generation=generation,
                integrity=integrity,
            )
        finally:
            await engine.dispose()

    asyncio.run(_run())


def _published_manifest_sha(
    s3: Any, bucket: str, track_id: uuid.UUID, generation: int
) -> str | None:
    """sha256 of the manifest at an existing generation, or None when absent."""
    try:
        body = s3.get_object(Bucket=bucket, Key=manifest_key(track_id, generation))["Body"]
        raw: bytes = body.read()
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise
    return manifest_sha256(raw)


# --- orchestration ------------------------------------------------------------


def ingest(
    *,
    s3: Any,
    bucket: str,
    source_ref: str,
    database_url: str | None,
    max_duration_seconds: float,
    dry_run: bool = False,
    workdir: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate, publish and register one drop-box import.

    Returns the result dict (also written to ``imports/{ref}/result.json``).
    Raises :class:`ImportNotReady` (no result written) while the drop is
    incomplete and :class:`ImportRejected` (result written, drop left intact)
    on any failed gate.
    """
    if not isinstance(source_ref, str) or SOURCE_REF_RE.fullmatch(source_ref) is None:
        # Refused before any S3 call: the namespace prefix keeps drop-box ids
        # disjoint from every other importer's refs.
        raise ValueError(
            "source_ref must match ^(youtube|sha256)-[A-Za-z0-9_-]{6,128}$, "
            f"got {source_ref!r}"
        )
    prefix = f"{IMPORTS_PREFIX}{source_ref}/"
    publisher = Publisher(s3, bucket)
    track_id = track_id_for_import(source_ref)
    at = now or datetime.now(UTC)

    def write_result(
        status: str,
        *,
        generation: int,
        code: str | None = None,
        issues: list[dict[str, Any]] | None = None,
        warnings: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": status,
            "sourceRef": source_ref,
            "trackId": str(track_id),
            "generation": generation,
            "s3Prefix": generation_prefix(track_id, generation).rstrip("/"),
            "manifestKey": manifest_key(track_id, generation),
            "code": code,
            "issues": issues or [],
            "warnings": warnings or [],
            "at": at.isoformat(),
        }
        s3.put_object(
            Bucket=bucket,
            Key=f"{prefix}{RESULT_NAME}",
            Body=json.dumps(result, indent=2, allow_nan=False).encode("utf-8"),
            ContentType="application/json",
        )
        return result

    def reject(
        code: str, issues: list[dict[str, Any]], generation: int = 1
    ) -> ImportRejected:
        return ImportRejected(code, issues, generation=generation)

    try:
        return _ingest_steps(
            s3=s3,
            bucket=bucket,
            prefix=prefix,
            source_ref=source_ref,
            publisher=publisher,
            track_id=track_id,
            database_url=database_url,
            max_duration_seconds=max_duration_seconds,
            dry_run=dry_run,
            workdir=workdir,
            write_result=write_result,
            reject=reject,
        )
    except ImportRejected as exc:
        # Every terminal rejection records itself next to the drop (step 14);
        # the dropped objects stay in place for inspection.
        exc.result = write_result(
            "rejected", generation=exc.generation, code=exc.code, issues=exc.issues
        )
        raise


def _ingest_steps(
    *,
    s3: Any,
    bucket: str,
    prefix: str,
    source_ref: str,
    publisher: Publisher,
    track_id: uuid.UUID,
    database_url: str | None,
    max_duration_seconds: float,
    dry_run: bool,
    workdir: Path | None,
    write_result: Callable[..., dict[str, Any]],
    reject: Callable[..., ImportRejected],
) -> dict[str, Any]:
    # 1. source_ref was validated by the caller before any S3 call.

    # 2. readiness: manifest.json arrives last (A1/C2 rule), so its absence
    #    means the producer is still writing — never read a half-dropped set.
    if _head_or_none(s3, bucket, f"{prefix}manifest.json") is None:
        raise ImportNotReady(f"{prefix}manifest.json is absent; the drop is not complete")

    # 3. parse the manifest bytes.
    raw: bytes = s3.get_object(Bucket=bucket, Key=f"{prefix}manifest.json")["Body"].read()
    dropped_sha = manifest_sha256(raw)
    try:
        manifest = json.loads(raw)
        if not isinstance(manifest, dict):
            raise ValueError("manifest.json must contain a JSON object")
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError) as exc:
        raise reject(
            "MANIFEST_INVALID", [_issue("manifest-json", f"manifest.json is unreadable: {exc}")]
        ) from exc

    # 4. shape gates.
    shape = validate_import_manifest(manifest, max_duration_seconds=max_duration_seconds)
    if has_errors(shape):
        raise reject(
            "MANIFEST_INVALID", [i.as_dict() for i in shape if i.severity == "error"]
        )
    expected = expected_media_files(manifest)

    # 5. existing identity (C4/C5): converge, never conflict silently. This
    #    runs BEFORE the inventory check on purpose — success cleanup (step
    #    13) deletes the seven dropped media objects, so a retry after a
    #    successful ingest must be recognized as already-published even
    #    though those objects are gone ("every rerun must converge").
    generation = 1
    if database_url:
        row = _get_track(database_url, track_id)
        if row is not None:
            if row.deleted_at is not None:
                # Never resurrect a soft-deleted track from a drop (C5).
                raise reject(
                    "TRACK_DELETED",
                    [_issue("track-deleted", "track row is soft-deleted; restore is explicit")],
                    generation=int(row.generation),
                )
            generation = int(row.generation)
            if _published_manifest_sha(s3, bucket, track_id, generation) == dropped_sha:
                return write_result("already-published", generation=generation)
            raise reject(
                "TRACK_CONFLICT",
                [
                    _issue(
                        "track-conflict",
                        "already published with different content; generation moves are "
                        "not a drop-box operation",
                    )
                ],
                generation=generation,
            )
    published_sha = _published_manifest_sha(s3, bucket, track_id, generation)
    if published_sha == dropped_sha:
        # The generation is complete and its manifest is byte-identical to
        # the drop: a DB-less rerun is already published; with a DB and no
        # row it is a crash between publish and register (a completed retry,
        # finished below without re-downloading or re-copying — the completed
        # generation proves a prior full validation).
        if database_url is None:
            return write_result("already-published", generation=generation)
        completed_retry = True
    else:
        if published_sha is not None:
            # The generation is immutable (C1) and holds different content.
            raise reject(
                "TRACK_CONFLICT",
                [
                    _issue(
                        "track-conflict",
                        f"generation {generation} is already published with different content",
                    )
                ],
                generation=generation,
            )
        completed_retry = False

    # 6. inventory: exactly the declared files, at the declared sizes — only
    #    for a drop that still needs publishing (already-published and
    #    completed-retry paths skip it; their media were deleted on purpose).
    if not completed_retry:
        problems = inventory_problems(publisher.list_prefix(prefix), expected)
        if problems:
            raise reject("INVENTORY_MISMATCH", [_issue("inventory", p) for p in problems])

    audits: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    publish_result: PublishResult | None = None
    tmp: tempfile.TemporaryDirectory[str] | None = None
    try:
        if not completed_retry:
            # 7. free-disk check before pulling the media.
            if workdir is not None:
                download_dir = Path(workdir)
            else:
                tmp = tempfile.TemporaryDirectory(prefix="browser-import-")
                download_dir = Path(tmp.name)
            download_dir.mkdir(parents=True, exist_ok=True)
            total_bytes = sum(size for size, _sha in expected.values())
            if shutil.disk_usage(download_dir).free < total_bytes + _DISK_HEADROOM_BYTES:
                raise reject(
                    "DISK_FULL",
                    [_issue("disk-full", "insufficient free space for the media plus headroom")],
                )

            # 8. download and re-prove the bytes (A4), then audit (C8).
            for file in sorted(expected):
                size, _sha = expected[file]
                _download_object(
                    s3, bucket, f"{prefix}{file}", download_dir / file, expected_size=size
                )
            audits = validate_downloaded_bytes(download_dir, manifest, expected)
            warnings = [
                i for a in audits for i in a.get("issues", []) if i.get("severity") == "warning"
            ]

            # 9. dry run: validated, nothing copied, nothing registered.
            if dry_run:
                return write_result("would-publish", generation=generation, warnings=warnings)

            # 10. stage by server-side copy, manifest LAST (C2).
            stage = staging_prefix(track_id, generation)
            staged: list[StagedObject] = []
            for file in sorted(expected):
                size, sha = expected[file]
                publisher.copy_object(
                    f"{prefix}{file}",
                    f"{stage}{file}",
                    size,
                    content_type="video/mp4" if file == "video.mp4" else "audio/mp4",
                )
                staged.append(StagedObject(file=file, size_bytes=size, sha256=sha))
            publisher.copy_object(
                f"{prefix}manifest.json",
                f"{stage}manifest.json",
                len(raw),
                content_type="application/json",
            )
            staged.append(StagedObject(file="manifest.json", size_bytes=len(raw), sha256=dropped_sha))

            # 11. guard, verify staging, promote (C1–C3). No-op when the
            #     generation manifest already exists.
            publish_result = publisher.publish(track_id, generation, staged)

            # A concurrent ingest of DIFFERENT content under the same ref may
            # have won this generation while we were validating: publish()
            # no-ops on an existing manifest (C1), so re-read what actually
            # landed and never register our manifest over a different one.
            # Always checked — not only when publish() reports already_published.
            landed_sha = _published_manifest_sha(s3, bucket, track_id, generation)
            if landed_sha != dropped_sha:
                raise reject(
                    "TRACK_CONFLICT",
                    [
                        _issue(
                            "track-conflict",
                            f"generation {generation} was published with different content "
                            "while this drop was being validated",
                        )
                    ],
                    generation=generation,
                )
    except PublishError as exc:
        # Staged verification / promotion failed: the bytes changed under us
        # or violate the format guard — a rejection, not a crash.
        raise reject("INTEGRITY_GATE_FAILED", [_issue("publisher", str(exc)[:400])]) from exc
    finally:
        if tmp is not None:
            tmp.cleanup()

    # 12. register the row (C5/C6/C8); title/artist resolve like any ingest.
    if database_url:
        # Close the identity window as far as the drop-box needs it: a row
        # that appeared meanwhile carrying a different recorded manifest hash
        # means a concurrent ingest won this ref. A residual window remains
        # between this read and the FOR UPDATE write; the drop-box is
        # single-operator today.
        current = _get_track(database_url, track_id)
        if current is not None and current.deleted_at is None:
            recorded = (current.integrity or {}).get("manifest_sha256")
            if isinstance(recorded, str) and recorded != dropped_sha:
                raise reject(
                    "TRACK_CONFLICT",
                    [
                        _issue(
                            "track-conflict",
                            "a concurrent ingest registered different content for this ref",
                        )
                    ],
                    generation=generation,
                )
        try:
            _register_track(
                database_url,
                track_id,
                generation,
                source_ref=source_ref,
                manifest=manifest,
                manifest_sha=dropped_sha,
                audits=audits,
                publish_result=publish_result,
            )
        except ImportConflict as exc:
            raise reject(
                "TRACK_CONFLICT", [_issue("track-conflict", str(exc))], generation=generation
            ) from exc

    # 13. success cleanup: staging objects and the seven dropped media files
    #     go away; manifest.json stays as provenance next to result.json.
    stage = staging_prefix(track_id, generation)
    for rel in publisher.list_prefix(stage):
        s3.delete_object(Bucket=bucket, Key=f"{stage}{rel}")
    for file in sorted(expected):
        s3.delete_object(Bucket=bucket, Key=f"{prefix}{file}")

    logger.info(
        "browser import %s -> %s (generation %d)",
        source_ref,
        generation_prefix(track_id, generation),
        generation,
    )
    return write_result("published", generation=generation, warnings=warnings)


# --- CLI ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--source-ref", required=True)
    parser.add_argument("--bucket", default=None, help="default: settings.s3_media_bucket")
    parser.add_argument("--database-url", default=None, help="default: settings.database_url")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--region", default=None, help="default: settings.aws_region")
    args = parser.parse_args(argv)

    from ..api.media import s3_client
    from ..settings import Settings

    settings = Settings(aws_region=args.region) if args.region else Settings()

    def emit(payload: dict[str, Any]) -> None:
        print(json.dumps(payload, allow_nan=False))

    try:
        result = ingest(
            s3=s3_client(settings),
            bucket=args.bucket or settings.s3_media_bucket,
            source_ref=args.source_ref,
            database_url=args.database_url or settings.database_url,
            max_duration_seconds=float(settings.max_duration_seconds),
            dry_run=args.dry_run,
            workdir=args.workdir,
        )
    except ValueError as exc:  # malformed source_ref — never reached S3
        emit({"status": "invalid-source-ref", "sourceRef": args.source_ref, "message": str(exc)})
        raise SystemExit(2) from None
    except ImportNotReady as exc:
        emit({"status": "not-ready", "sourceRef": args.source_ref, "message": str(exc)})
        raise SystemExit(3) from None
    except ImportRejected as exc:
        emit(
            exc.result
            or {
                "status": "rejected",
                "sourceRef": args.source_ref,
                "code": exc.code,
                "issues": exc.issues,
            }
        )
        raise SystemExit(2) from None
    emit(result)
    raise SystemExit(0)


if __name__ == "__main__":
    main()

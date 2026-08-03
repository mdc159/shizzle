#!/usr/bin/env python
"""Ingest an existing video/audio source into the Shizzle library as fresh stems.

Reusable "source in -> stems in the library" driver for the Phase 3 cloud
pipeline. Given a source it:

1. **Stages** a browser/worker-ready ``source.mp4`` at
   ``tracks/{track_id}/1/staging/source.mp4`` (server-side S3 copy when the
   source is already an object in the bucket with an audio track; else it builds
   the file locally and uploads it).
2. **Submits** a RunPod serverless job (``RUNPOD_ENDPOINT_ID``) and polls it to a
   terminal state with backoff — cold start + Demucs weights download can take
   several minutes, so the timeout is generous and every status transition is
   logged. The RunPod job id is persisted so a run interrupted mid-flight
   resumes instead of resubmitting.
3. **Publishes** on success with the exact server code
   (``shizzle_server.publish.Publisher``): verifies the six staged stems +
   video + manifest against the worker's reported checksums, promotes
   staging -> ``tracks/{track_id}/1/`` by server-side copy (manifest last), then
   writes the ``tracks`` row via ``TrackRepository.upsert_imported`` — the same
   repository the library importer uses.

Idempotent and resumable throughout: track ids are deterministic
(``track_id_for_import(folder)``), an already-published generation is a no-op,
and a staged source / in-flight job is reused rather than recreated.

The AC/DC recovery (``--all-lost``)
-----------------------------------
The four lost tracks in ``spikes/RESULTS-legacy-import.md`` (§3, "Complete twin
elsewhere? = no") kept only ``video.mp4`` and ``stems/stems_merged.webm`` after
their six ``.m4a`` stems went missing from S3. Crucially the legacy
``video.mp4`` is **video-only (h264, no audio)** — re-splitting it alone would
yield silence. So the recovery muxes the surviving video (copied) with the
surviving pre-mixed audio (opus in ``stems_merged.webm``, transcoded to AAC)
into a single ``source.mp4``, and re-separates that. This transcode is mildly
lossy (opus -> aac -> Demucs) but recovers content that was otherwise gone.

Usage
-----
    # plan only, no S3 / RunPod / DB writes
    uv run --directory server python ../scripts/resplit_track.py --all-lost --dry-run

    # recover all four lost AC/DC tracks
    uv run --directory server python ../scripts/resplit_track.py --all-lost

    # one folder (legacy recover) or an arbitrary source
    ... --folder 3ae816991eb4
    ... --local-file path/to/video.mp4 --title "Song" --artist "Artist"
    ... --source-key some/existing/video-with-audio.mp4 --title "Song"

Credentials come from ``.env`` (real AWS keys; the machine-wide R2
``AWS_ENDPOINT_URL`` override is force-cleared so S3 calls hit AWS). The compose
``DATABASE_URL`` host is the internal service name ``postgres`` — unreachable
from the host — so the DB DSN defaults to the exposed stack Postgres on
``127.0.0.1:5434``; override with ``--database-url``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "server" / "src"))

import boto3  # noqa: E402
import httpx  # noqa: E402

from shizzle_server.db import create_engine, create_session_factory  # noqa: E402
from shizzle_server.db.repository import (  # noqa: E402
    TrackRepository,
    track_id_for_import,
)
from shizzle_server.publish import (  # noqa: E402
    ChecksumPolicy,
    Publisher,
    StagedObject,
    generation_prefix,
    manifest_key,
    staging_prefix,
)

logger = logging.getLogger("resplit")

# --- constants ----------------------------------------------------------------

#: The working media bucket. The Phase 3 `shizzle-media` bucket does not exist
#: yet (NoSuchBucket); the legacy import wrote `tracks/` into this bucket and
#: that is where the 27 imported generations live, so re-splits land beside them.
DEFAULT_BUCKET = "karaoke-pimpshizzle"
DEFAULT_LEGACY_PREFIX = "karaoke/pub/"
GENERATION = 1
RUNPOD_BASE = "https://api.runpod.ai/v2"
TERMINAL = {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
STEM_COUNT = 6  # htdemucs_6s

#: Stamped on the tracks row; the worker's gates ran for real, unlike the
#: legacy import, so this is not `gates: not-run`.
RESPLIT_SOURCE = "resplit-runpod"


# --- env ----------------------------------------------------------------------


def main_checkout_root() -> Path:
    """The primary checkout, even when running from a `.claude/worktrees/...` copy.

    `.env` and other machine-local, git-ignored files live only in the main
    checkout, not in an isolated worktree.
    """
    parts = str(REPO_ROOT).replace("\\", "/")
    marker = "/.claude/worktrees/"
    if marker in parts:
        return Path(parts.split(marker, 1)[0])
    return REPO_ROOT


def find_env_file(explicit: str | None) -> Path | None:
    if explicit:
        return Path(explicit)
    for cand in (REPO_ROOT / ".env", main_checkout_root() / ".env"):
        if cand.exists():
            return cand
    return None


def load_env_file(path: Path | None) -> None:
    """Load KEY=VALUE lines, overwriting the ambient environment.

    `.env` carries the real AWS keys; the ambient environment on this machine
    holds an R2 override (keys + endpoint) that must not win. `.env` is not pure
    ASCII, hence the explicit utf-8 decode.
    """
    if path is None or not path.exists():
        logger.warning("no .env found; relying on the ambient environment")
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
    # The machine-wide R2 endpoint override must never reach these AWS calls.
    os.environ.pop("AWS_ENDPOINT_URL", None)
    os.environ.pop("AWS_ENDPOINT_URL_S3", None)
    return boto3.client("s3", region_name=region)


def resolve_database_url(explicit: str | None, host: str, port: int) -> str:
    """DSN for the stack Postgres.

    `.env`'s DATABASE_URL points at the compose-internal host `postgres`, which
    does not resolve from the host box. Default to the exposed port instead,
    built from the POSTGRES_* credentials.
    """
    if explicit:
        return explicit
    user = os.environ.get("POSTGRES_USER", "shizzle")
    pw = os.environ.get("POSTGRES_PASSWORD", "")
    db = os.environ.get("POSTGRES_DB", "shizzle")
    return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{db}"


# --- lost-track parsing -------------------------------------------------------


@dataclass
class SourcePlan:
    """A track to (re)build and where its source material lives."""

    key: str  # stable id used for track_id + state (folder name, or a slug)
    title: str
    artist: str
    mode: str  # "legacy-recover" | "s3-copy" | "local-upload"
    folder: str | None = None  # legacy-recover: karaoke/pub/{folder}/
    source_key: str | None = None  # s3-copy: existing object key (needs audio)
    local_file: str | None = None  # local-upload: path on disk


def parse_lost_tracks(results_path: Path) -> list[SourcePlan]:
    """Read the four genuinely-lost folders from RESULTS-legacy-import.md §3.

    The table row is ``| `folder` | Artist — Title | twin |``; a track is lost
    when the twin column says "no". Falls back to the documented four if the
    table cannot be parsed.
    """
    fallback = [
        SourcePlan("3ae816991eb4", "For Those About To Rock", "AC/DC", "legacy-recover", folder="3ae816991eb4"),
        SourcePlan("87eedd1c4ec2", "Dirty Deeds Done Dirt Cheap", "AC/DC", "legacy-recover", folder="87eedd1c4ec2"),
        SourcePlan("cdace71ff551", "Dirty Deeds (v3 MERGED)", "AC/DC", "legacy-recover", folder="cdace71ff551"),
        SourcePlan("de330790cdfa", "Highway to Hell", "AC/DC", "legacy-recover", folder="de330790cdfa"),
    ]
    if not results_path.exists():
        logger.warning("RESULTS file %s not found; using documented lost list", results_path)
        return fallback

    row = re.compile(
        r"^\|\s*`?([0-9a-f]{12})`?\s*\|\s*([^|]+?)\s*\|\s*([^|]*?)\s*\|\s*$"
    )
    found: list[SourcePlan] = []
    for line in results_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = row.match(line.strip())
        if not m:
            continue
        folder, title_cell, twin = m.group(1), m.group(2).strip(), m.group(3).strip().lower()
        # twin column "no" (possibly wrapped in ** or with trailing note) = lost.
        twin_no = re.sub(r"[^a-z]", "", twin.split()[0] if twin else "") == "no"
        if not twin_no:
            continue
        artist, _, song = title_cell.partition("—")
        if not song:  # try ASCII hyphen fallback
            artist, _, song = title_cell.partition(" - ")
        artist = artist.strip() or "Unknown"
        title = song.strip() or title_cell
        found.append(SourcePlan(folder, title, artist, "legacy-recover", folder=folder))
    if not found:
        logger.warning("could not parse lost folders from %s; using documented list", results_path)
        return fallback
    logger.info("parsed %d lost tracks from %s", len(found), results_path.name)
    return found


# --- source staging -----------------------------------------------------------


def probe_has_audio(s3: Any, bucket: str, key: str) -> bool:
    url = s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=600)
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries",
         "stream=codec_type", "-of", "csv=p=0", url],
        capture_output=True, text=True,
    )
    return "audio" in out.stdout


def _probe_audio_channels(path: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=channels", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True,
    )
    try:
        return int(out.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return 0


def _stem_pairs_to_stereo_filter(channels: int) -> str | None:
    """A pan expression summing N stereo stem pairs back into the full mix.

    The k25 ``stems_merged.webm`` is not a stereo pre-mix: it packs the six
    stems as consecutive stereo channel pairs (12 channels; the legacy
    ``channel_offset`` was 0,2,4,6,8,10). Demucs stems sum to their input, so
    summing every left channel and every right channel reconstructs the original
    stereo mix — the correct thing to re-separate. Returns None for a source
    that is already stereo/mono (no reconstruction needed).
    """
    if channels <= 2 or channels % 2 != 0:
        return None
    lefts = "+".join(f"c{i}" for i in range(0, channels, 2))
    rights = "+".join(f"c{i}" for i in range(1, channels, 2))
    return f"[1:a]pan=stereo|c0={lefts}|c1={rights}[a]"


def build_recovery_source(
    s3: Any, legacy_bucket: str, legacy_prefix: str, folder: str, workdir: Path
) -> Path:
    """Mux surviving video (video-only h264) + reconstructed stereo mix -> source.mp4.

    The audio survives only as the multi-channel ``stems_merged.webm`` (the six
    stems as channel pairs); it is downmixed back to the stereo mix before the
    worker re-separates it.
    """
    video_key = f"{legacy_prefix}{folder}/video.mp4"
    audio_key = f"{legacy_prefix}{folder}/stems/stems_merged.webm"
    local_video = workdir / "video.mp4"
    local_audio = workdir / "merged.webm"
    logger.info("  downloading legacy video + merged audio for %s", folder)
    s3.download_file(legacy_bucket, video_key, str(local_video))
    s3.download_file(legacy_bucket, audio_key, str(local_audio))

    channels = _probe_audio_channels(local_audio)
    pan = _stem_pairs_to_stereo_filter(channels)
    source = workdir / "source.mp4"
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", str(local_video), "-i", str(local_audio)]
    if pan is not None:
        logger.info("  reconstructing stereo mix from %d-channel merged audio", channels)
        cmd += ["-filter_complex", pan, "-map", "0:v:0", "-map", "[a]"]
    else:
        cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    cmd += ["-c:v", "copy", "-c:a", "aac", "-b:a", "320k",
            "-movflags", "+faststart", str(source)]
    logger.info("  muxing video + audio -> source.mp4")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not source.exists():
        raise RuntimeError(f"ffmpeg mux failed for {folder}: {res.stderr[:300]}")
    return source


def stage_source(
    s3: Any,
    bucket: str,
    legacy_bucket: str,
    legacy_prefix: str,
    plan: SourcePlan,
    track_id: uuid.UUID,
    workdir: Path,
) -> str:
    """Ensure tracks/{id}/1/staging/source.mp4 exists. Returns the staged key."""
    dst_key = f"{staging_prefix(track_id, GENERATION)}source.mp4"
    # Resumable: reuse a source already staged.
    try:
        s3.head_object(Bucket=bucket, Key=dst_key)
        logger.info("  staged source already present, reusing: %s", dst_key)
        return dst_key
    except Exception:
        pass

    if plan.mode == "s3-copy":
        assert plan.source_key
        if not probe_has_audio(s3, legacy_bucket, plan.source_key):
            raise RuntimeError(
                f"source {plan.source_key} has no audio stream; cannot re-split it directly"
            )
        s3.copy_object(
            Bucket=bucket, Key=dst_key,
            CopySource={"Bucket": legacy_bucket, "Key": plan.source_key},
        )
        logger.info("  server-side copied source -> %s", dst_key)
        return dst_key

    if plan.mode == "local-upload":
        assert plan.local_file
        local = Path(plan.local_file)
        if not local.exists():
            raise FileNotFoundError(f"local source not found: {local}")
        s3.upload_file(str(local), bucket, dst_key, ExtraArgs={"ContentType": "video/mp4"})
        logger.info("  uploaded local source -> %s", dst_key)
        return dst_key

    # legacy-recover
    assert plan.folder
    source = build_recovery_source(s3, legacy_bucket, legacy_prefix, plan.folder, workdir)
    s3.upload_file(str(source), bucket, dst_key, ExtraArgs={"ContentType": "video/mp4"})
    logger.info("  uploaded recovered source (%.1f MiB) -> %s",
                source.stat().st_size / 1024**2, dst_key)
    return dst_key


# --- runpod -------------------------------------------------------------------


def runpod_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['RUNPOD_API_KEY']}",
            "Content-Type": "application/json"}


async def runpod_health(client: httpx.AsyncClient, eid: str) -> dict[str, Any] | None:
    try:
        r = await client.get(f"{RUNPOD_BASE}/{eid}/health", headers=runpod_headers(), timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        logger.warning("runpod health check failed: %s", exc)
    return None


async def runpod_submit(
    client: httpx.AsyncClient, eid: str, job_input: dict[str, Any], retries: int = 4
) -> str:
    """POST /run; returns the RunPod job id. Retries transient failures."""
    delay = 3.0
    last_err = "unknown"
    for attempt in range(1, retries + 1):
        try:
            r = await client.post(
                f"{RUNPOD_BASE}/{eid}/run", headers=runpod_headers(),
                json={"input": job_input}, timeout=60,
            )
            if r.status_code in (200, 201):
                data = r.json()
                jid = data.get("id")
                if jid:
                    logger.info("  submitted runpod job %s (status %s)", jid, data.get("status"))
                    return jid
                last_err = f"no id in response: {data}"
            else:
                last_err = f"HTTP {r.status_code}: {r.text[:200]}"
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
        logger.warning("  submit attempt %d/%d failed: %s", attempt, retries, last_err)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)
    raise RuntimeError(f"runpod submit failed after {retries} attempts: {last_err}")


async def runpod_poll(
    client: httpx.AsyncClient, eid: str, job_id: str, timeout_s: float
) -> dict[str, Any]:
    """Poll /status/{id} to a terminal state with backoff.

    Returns the final status payload (COMPLETED carries `output`). On local
    timeout returns {status: "IN_PROGRESS", timed_out_locally: True} so the
    caller can persist the job id and resume later instead of blocking.
    """
    headers = runpod_headers()
    started = time.monotonic()
    deadline = started + timeout_s
    delay = 5.0
    last_status: str | None = None
    consecutive_errors = 0
    while time.monotonic() < deadline:
        try:
            r = await client.get(f"{RUNPOD_BASE}/{eid}/status/{job_id}", headers=headers, timeout=30)
            consecutive_errors = 0
            if r.status_code != 200:
                logger.warning("  status HTTP %d: %s", r.status_code, r.text[:150])
                await asyncio.sleep(delay)
                continue
            data = r.json()
        except Exception as exc:
            consecutive_errors += 1
            logger.warning("  status poll error (%d): %s", consecutive_errors, exc)
            await asyncio.sleep(min(delay * consecutive_errors, 30))
            continue

        status = data.get("status")
        if status != last_status:
            logger.info("  runpod %s: %s -> %s (%.0fs elapsed)",
                        job_id, last_status or "-", status, time.monotonic() - started)
            last_status = status
        if status in TERMINAL:
            return data
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 30.0)

    logger.warning("  local poll timeout (%.0fs) for %s; last status %s — resumable",
                   timeout_s, job_id, last_status)
    return {"status": last_status or "IN_QUEUE", "timed_out_locally": True}


# --- outcome / state ----------------------------------------------------------


@dataclass
class Outcome:
    key: str
    track_id: str
    title: str
    artist: str
    status: str  # dry-run|already-published|completed|published|queued|running|failed
    runpod_job_id: str | None = None
    staged_source_key: str | None = None
    published: bool = False
    row_status: str = "pending"  # created|updated|pending-db-unreachable|pending
    objects_verified: int = 0
    duration: float | None = None
    s3_outputs: list[str] = field(default_factory=list)
    reason: str | None = None
    elapsed_s: float = 0.0


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("could not read state %s: %s", path, exc)
    return {}


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


# --- per-track flow -----------------------------------------------------------


def _init_outcome(plan: SourcePlan, state: dict[str, dict[str, Any]]) -> tuple[uuid.UUID, Outcome]:
    track_id = track_id_for_import(plan.key)
    outcome = Outcome(
        key=plan.key, track_id=str(track_id), title=plan.title, artist=plan.artist,
        status="failed",
    )
    prior = state.get(plan.key, {})
    if prior.get("runpod_job_id"):
        outcome.runpod_job_id = prior["runpod_job_id"]
    return track_id, outcome


async def submit_track(
    plan: SourcePlan,
    args: argparse.Namespace,
    s3: Any,
    publisher: Publisher,
    client: httpx.AsyncClient,
    state: dict[str, dict[str, Any]],
    index: int,
    total: int,
) -> Outcome:
    """Phase A: stage the source and submit (or resume) the RunPod job.

    Does not poll — so every one of the N jobs is running on the endpoint's
    workers before any long poll begins, and every job id is persisted first.
    """
    track_id, outcome = _init_outcome(plan, state)
    prior = state.get(plan.key, {})
    label = f"[{index}/{total}] {plan.key} {plan.title!r}"

    if args.dry_run:
        outcome.status = "dry-run"
        src = {
            "legacy-recover": f"mux {args.legacy_prefix}{plan.folder}/(video.mp4 + stems/stems_merged.webm)",
            "s3-copy": f"server-side copy {plan.source_key}",
            "local-upload": f"upload {plan.local_file}",
        }[plan.mode]
        logger.info("%s DRY-RUN\n    track_id=%s\n    source: %s\n    -> tracks/%s/%d/staging/source.mp4"
                    "\n    then RunPod split + publish + tracks row",
                    label, track_id, src, track_id, GENERATION)
        return outcome

    try:
        # Already published? Generation is immutable — mark for the finish pass.
        if publisher.is_published(track_id, GENERATION):
            logger.info("%s already published (manifest present) — S3 untouched", label)
            outcome.status = "already-published"
            outcome.published = True
            _persist(state, args, plan, outcome)
            return outcome

        # Stage the source (mux/copy/upload). Idempotent: reuses a staged source.
        with tempfile.TemporaryDirectory(prefix=f"resplit-{plan.key}-") as td:
            outcome.staged_source_key = stage_source(
                s3, args.bucket, args.legacy_bucket, args.legacy_prefix, plan, track_id, Path(td)
            )

        # Submit (or resume) the RunPod job.
        job_id = outcome.runpod_job_id
        if job_id and prior.get("status") in ("queued", "running", "submitted") and not args.force_resubmit:
            logger.info("%s resuming existing runpod job %s", label, job_id)
            outcome.status = prior.get("status", "submitted")
        else:
            job_input = {
                "job_id": f"resplit-{plan.key}",
                "bucket": args.bucket,
                "input_key": outcome.staged_source_key,
                "output_prefix": staging_prefix(track_id, GENERATION),
                "model": args.model,
                "create_multitrack_mp4": args.multitrack,
                "metadata": {
                    "title": plan.title, "artist": plan.artist,
                    "sourceUrl": "", "videoId": "",
                },
            }
            outcome.runpod_job_id = await runpod_submit(client, args.endpoint_id, job_input)
            outcome.status = "submitted"
        _persist(state, args, plan, outcome)
    except Exception as exc:
        outcome.status = "failed"
        outcome.reason = f"stage/submit: {type(exc).__name__}: {exc}"[:300]
        _persist(state, args, plan, outcome)
        logger.exception("%s stage/submit FAILED", label)
    return outcome


async def finish_track(
    plan: SourcePlan,
    outcome: Outcome,
    args: argparse.Namespace,
    publisher: Publisher,
    client: httpx.AsyncClient,
    session_factory: Any,
    state: dict[str, dict[str, Any]],
    index: int,
    total: int,
) -> Outcome:
    """Phase B: poll the job to terminal, then publish + write the row."""
    track_id = track_id_for_import(plan.key)
    prior = state.get(plan.key, {})
    label = f"[{index}/{total}] {plan.key} {plan.title!r}"
    started = time.monotonic()

    # Nothing to finish for jobs that never submitted (failed) in phase A.
    if outcome.status == "already-published":
        await _write_row(outcome, track_id, publisher, args, session_factory, prior)
        _persist(state, args, plan, outcome)
        return outcome
    if outcome.status == "failed" or not outcome.runpod_job_id:
        return outcome

    try:
        result = await runpod_poll(client, args.endpoint_id, outcome.runpod_job_id, args.poll_timeout)
        status = result.get("status")
        if result.get("timed_out_locally"):
            outcome.status = "running" if status in ("IN_PROGRESS", "RUNNING") else "queued"
            outcome.reason = f"poll timed out locally; job {outcome.runpod_job_id} still {status}"
            _persist(state, args, plan, outcome)
            logger.warning("%s NOT DONE (%s) — job id recorded, resumable", label, outcome.status)
            return outcome
        if status != "COMPLETED":
            outcome.status = "failed"
            outcome.reason = f"runpod {status}: {str(result.get('error'))[:200]}"
            _persist(state, args, plan, outcome)
            logger.error("%s RunPod FAILED: %s", label, outcome.reason)
            return outcome

        # Publish: verify staged output + promote (server code, manifest last).
        payload = result.get("output") or {}
        reported = StagedObject.from_result_payload(payload)
        if not reported:
            outcome.status = "failed"
            outcome.reason = "runpod COMPLETED but output has no uploads[]"
            _persist(state, args, plan, outcome)
            logger.error("%s %s", label, outcome.reason)
            return outcome
        pub = publisher.publish(track_id, GENERATION, reported)
        outcome.published = True
        outcome.status = "published"
        outcome.objects_verified = pub.verification.sha256_verified if pub.verification else 0

        # Verify destination structure.
        landed = publisher.list_prefix(generation_prefix(track_id, GENERATION))
        outcome.s3_outputs = sorted(landed)
        stems = [k for k in landed if k.startswith("stems/") and k.endswith(".m4a")]
        problems = []
        if "manifest.json" not in landed:
            problems.append("manifest.json missing")
        if "video.mp4" not in landed:
            problems.append("video.mp4 missing")
        if len(stems) < STEM_COUNT:
            problems.append(f"only {len(stems)}/{STEM_COUNT} stems")
        if problems:
            outcome.status = "failed"
            outcome.reason = "; ".join(problems)
            _persist(state, args, plan, outcome)
            logger.error("%s VERIFY FAILED: %s", label, outcome.reason)
            return outcome

        # Write the tracks row.
        await _write_row(outcome, track_id, publisher, args, session_factory, prior,
                         worker_integrity=payload.get("integrity"),
                         publisher_report=pub.verification.to_integrity() if pub.verification else None)
        outcome.elapsed_s = round(time.monotonic() - started, 2)
        _persist(state, args, plan, outcome)
        logger.info("%s PUBLISHED: %d objects, %d stems, %.1fs (row %s)",
                    label, len(landed), len(stems), outcome.elapsed_s, outcome.row_status)
    except Exception as exc:
        outcome.status = "failed"
        outcome.reason = f"finish: {type(exc).__name__}: {exc}"[:300]
        _persist(state, args, plan, outcome)
        logger.exception("%s finish FAILED", label)
    return outcome


async def _write_row(
    outcome: Outcome,
    track_id: uuid.UUID,
    publisher: Publisher,
    args: argparse.Namespace,
    session_factory: Any,
    prior: dict[str, Any],
    worker_integrity: Any = None,
    publisher_report: Any = None,
) -> None:
    """Read the promoted manifest for metadata and upsert the tracks row.

    If the DB is unreachable, the S3 side is still done and verified — the row
    is marked pending rather than failing the whole recovery.
    """
    manifest = {}
    try:
        body = publisher.s3.get_object(
            Bucket=args.bucket, Key=manifest_key(track_id, GENERATION)
        )["Body"].read()
        manifest = json.loads(body)
    except Exception as exc:
        logger.warning("  could not read promoted manifest: %s", exc)

    title = manifest.get("title") or outcome.title
    artist = manifest.get("artist") or outcome.artist
    duration = float(manifest.get("duration", 0.0) or 0.0)
    outcome.duration = duration
    integrity: dict[str, Any] = {
        "source": RESPLIT_SOURCE,
        "legacy_folder": outcome.key,
        "runpod_job_id": outcome.runpod_job_id or prior.get("runpod_job_id"),
    }
    if worker_integrity is not None:
        integrity["worker"] = worker_integrity
    if publisher_report is not None:
        integrity["publisher"] = publisher_report

    if session_factory is None:
        outcome.row_status = "pending-db-unreachable"
        logger.warning("  DB unreachable — S3 done, tracks row PENDING for %s", track_id)
        return
    try:
        repo = TrackRepository(session_factory)
        _, created = await repo.upsert_imported(
            track_id,
            title=title, artist=artist, duration_seconds=duration,
            s3_prefix=generation_prefix(track_id, GENERATION).rstrip("/"),
            manifest_key=manifest_key(track_id, GENERATION),
            generation=GENERATION, integrity=integrity,
        )
        outcome.row_status = "created" if created else "updated"
    except Exception as exc:
        outcome.row_status = "pending-db-unreachable"
        logger.warning("  tracks row write failed (%s) — S3 done, row PENDING", exc)


def _persist(state: dict[str, dict[str, Any]], args: argparse.Namespace,
             plan: SourcePlan, outcome: Outcome) -> None:
    state[plan.key] = asdict(outcome)
    save_state(args.state, state)


# --- run ----------------------------------------------------------------------


async def run(args: argparse.Namespace, plans: list[SourcePlan]) -> list[Outcome]:
    s3 = s3_client(args.region)
    publisher = Publisher(s3, args.bucket, checksum_policy=ChecksumPolicy(args.checksum_policy))
    state = load_state(args.state)

    session_factory = None
    engine = None
    if not args.dry_run and not args.no_db:
        try:
            engine = create_engine(args.database_url)
            session_factory = create_session_factory(engine)
            # Fail fast so we know whether rows will land.
            from sqlalchemy import text
            async with engine.connect() as c:
                await c.execute(text("select 1"))
            logger.info("DB connected: rows will be written")
        except Exception as exc:
            logger.warning("DB unreachable (%s); S3 will be done, rows marked pending", exc)
            session_factory = None
            if engine is not None:
                await engine.dispose()
                engine = None

    outcomes: list[Outcome] = []
    async with httpx.AsyncClient() as client:
        if not args.dry_run:
            health = await runpod_health(client, args.endpoint_id)
            if health:
                w = health.get("workers", {})
                logger.info("RunPod endpoint healthy: workers ready=%s idle=%s, jobs %s",
                            w.get("ready"), w.get("idle"), health.get("jobs"))
            else:
                logger.warning("RunPod health unknown — submitting anyway, polling robustly")
        try:
            if args.dry_run:
                for i, plan in enumerate(plans, 1):
                    outcomes.append(
                        await submit_track(plan, args, s3, publisher, client,
                                           state, i, len(plans))
                    )
                return outcomes
            # Phase A: stage + submit every job first (so all run concurrently
            # on the endpoint's workers and every job id is persisted up front).
            logger.info("=== phase A: staging + submitting %d job(s) ===", len(plans))
            submitted: list[tuple[SourcePlan, Outcome]] = []
            for i, plan in enumerate(plans, 1):
                oc = await submit_track(plan, args, s3, publisher, client,
                                        state, i, len(plans))
                submitted.append((plan, oc))
            # Phase B: poll each to terminal, then publish + write row.
            logger.info("=== phase B: polling + publishing ===")
            for i, (plan, oc) in enumerate(submitted, 1):
                outcomes.append(
                    await finish_track(plan, oc, args, publisher, client,
                                       session_factory, state, i, len(plans))
                )
        finally:
            if engine is not None:
                await engine.dispose()
    return outcomes


# --- cli ----------------------------------------------------------------------


def build_plans(args: argparse.Namespace) -> list[SourcePlan]:
    if args.all_lost:
        return parse_lost_tracks(args.results)
    if args.folder:
        lost = {p.folder: p for p in parse_lost_tracks(args.results) if p.folder}
        if args.folder in lost:
            p = lost[args.folder]
            if args.title:
                p.title = args.title
            if args.artist:
                p.artist = args.artist
            return [p]
        return [SourcePlan(args.folder, args.title or args.folder, args.artist or "Unknown",
                           "legacy-recover", folder=args.folder)]
    if args.local_file:
        key = args.key or Path(args.local_file).stem
        return [SourcePlan(key, args.title or key, args.artist or "Unknown",
                           "local-upload", local_file=args.local_file)]
    if args.source_key:
        key = args.key or Path(args.source_key).stem
        return [SourcePlan(key, args.title or key, args.artist or "Unknown",
                           "s3-copy", source_key=args.source_key)]
    return []


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    src = p.add_argument_group("source selection (choose one)")
    src.add_argument("--all-lost", action="store_true",
                     help="recover the four lost AC/DC tracks from RESULTS-legacy-import.md")
    src.add_argument("--folder", help="single legacy folder to recover (video+merged mux)")
    src.add_argument("--local-file", help="upload a local video file as the source")
    src.add_argument("--source-key", help="existing S3 object (must have audio) to copy as source")
    p.add_argument("--key", help="stable id override for --local-file/--source-key (defaults to stem)")
    p.add_argument("--title", help="track title metadata")
    p.add_argument("--artist", help="track artist metadata")

    p.add_argument("--bucket", default=DEFAULT_BUCKET, help="media bucket for tracks/ (default: %(default)s)")
    p.add_argument("--legacy-bucket", default=DEFAULT_BUCKET, help="bucket holding the legacy source objects")
    p.add_argument("--legacy-prefix", default=DEFAULT_LEGACY_PREFIX)
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    p.add_argument("--results", type=Path,
                   default=REPO_ROOT / "spikes" / "RESULTS-legacy-import.md")

    p.add_argument("--endpoint-id", default=None, help="RunPod endpoint id (default: $RUNPOD_ENDPOINT_ID)")
    p.add_argument("--model", default="htdemucs_6s")
    p.add_argument("--multitrack", action="store_true", default=True)
    p.add_argument("--no-multitrack", dest="multitrack", action="store_false")
    p.add_argument("--poll-timeout", type=float, default=1800.0,
                   help="seconds to poll one job before giving up and recording it resumable")
    p.add_argument("--force-resubmit", action="store_true",
                   help="submit a new RunPod job even if one is recorded in state")
    p.add_argument("--checksum-policy", default="auto",
                   choices=[c.value for c in ChecksumPolicy])

    p.add_argument("--database-url", default=None, help="async DSN; default: stack Postgres on 127.0.0.1")
    p.add_argument("--db-host", default="127.0.0.1")
    p.add_argument("--db-port", type=int, default=5434)
    p.add_argument("--no-db", action="store_true", help="skip the DB row (S3 only)")

    p.add_argument("--state", type=Path, default=REPO_ROOT / "spikes" / "resplit-state.json",
                   help="resumability state file")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--env-file", default=None)
    p.add_argument("--report", type=Path, default=None, help="write a JSON run report here")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    load_env_file(find_env_file(args.env_file))
    args.endpoint_id = args.endpoint_id or os.environ.get("RUNPOD_ENDPOINT_ID", "")
    args.database_url = resolve_database_url(args.database_url, args.db_host, args.db_port)

    plans = build_plans(args)
    if not plans:
        logger.error("no source selected: use --all-lost / --folder / --local-file / --source-key")
        return 2
    if not args.dry_run and not args.endpoint_id:
        logger.error("no RunPod endpoint id (--endpoint-id or $RUNPOD_ENDPOINT_ID)")
        return 2

    started = time.monotonic()
    outcomes = asyncio.run(run(args, plans))
    elapsed = time.monotonic() - started

    counts: dict[str, int] = {}
    for o in outcomes:
        counts[o.status] = counts.get(o.status, 0) + 1
    print("\n=== resplit summary ===")
    for status, n in sorted(counts.items()):
        print(f"  {status:20s} {n}")
    print(f"  {'elapsed':20s} {elapsed:.1f}s")
    for o in outcomes:
        print(f"  {o.key}  {o.status:16s} job={o.runpod_job_id or '-'}  row={o.row_status}"
              f"  track_id={o.track_id}")
        if o.reason:
            print(f"      reason: {o.reason}")

    if args.report:
        args.report.write_text(
            json.dumps({"elapsed_s": round(elapsed, 2), "counts": counts,
                        "outcomes": [asdict(o) for o in outcomes]}, indent=2),
            encoding="utf-8",
        )
        print(f"  report -> {args.report}")

    unresolved = [o for o in outcomes if o.status in ("failed",)]
    return 1 if unresolved else 0


if __name__ == "__main__":
    raise SystemExit(main())

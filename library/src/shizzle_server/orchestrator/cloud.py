"""Cloud-pipeline stage helpers (WS1 source upload + WS5 verify/publish).

These run only when ``Settings.cloud_pipeline`` is true (cloud mode). In
local/test mode the orchestrator never calls them — the stage handlers branch
at the top on ``ctx.settings.cloud_pipeline`` (wired in WS4/WS5 once B2's
``cloud_pipeline`` property lands).

Spec: approved plan ``async-wiggling-yao.md`` design decisions 1, 2, 4, 5, 6.
    decision 2 — source to S3 via boto3 in a thread, idempotent on size match.
    decision 5 — package intake re-proves the worker's handoff.json.
    decision 6 — stage-level branching; no CloudPipeline class. These helpers
                 are the cloud branch of the existing verify/publish handlers.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..api.media import s3_client
from ..db.models import JobStage
from ..db.repository import track_id_for_job
from ..errors import ErrorCode, StageError
from ..publish.lossless_intake import (
    IntakeError,
    PackageNotReady,
    download_package,
    load_and_verify_package,
    stage,
    transform,
)
from ..publish.publisher import Publisher, PublishError

if TYPE_CHECKING:  # avoid a runtime import cycle with stages.py
    from .stages import StageContext

logger = logging.getLogger(__name__)

GENERATION = 1
# The lossless worker writes its outputs under this deterministic prefix (no
# result payload needed to find the package — decision 3).
SEPARATION_SEGMENT = "separation"


def source_key(track_id: uuid.UUID | str) -> str:
    """Private upload source outside the browser cookie's ``tracks/*`` scope."""
    return f"sources/{track_id}/source.mp4"


def separation_prefix(track_id: uuid.UUID | str) -> str:
    """Where the worker drops the lossless-stem-v1 package + handoff.json."""
    return f"tracks/{track_id}/{GENERATION}/{SEPARATION_SEGMENT}"


async def package_ready(ctx: StageContext) -> bool:
    """Check the handoff-last marker for a dispatch whose RunPod id was lost."""
    track_id = track_id_for_job(ctx.job.id)
    key = f"{separation_prefix(track_id)}/handoff.json"
    try:
        head = await asyncio.to_thread(
            _head_or_none,
            s3_client(ctx.settings),
            ctx.settings.s3_media_bucket,
            key,
        )
    except Exception as exc:
        raise StageError(
            ErrorCode.S3_UPLOAD_FAILED,
            f"dispatch reconciliation failed to inspect {key}: {exc}"[:500],
            retryable=True,
        ) from exc
    return head is not None


# --- WS1: upload source to S3 ------------------------------------------------


async def upload_source(ctx: StageContext) -> str:
    """Upload the source video to S3 (idempotent on size match).

    Raises ``StageError(SOURCE_MISSING, retryable=False)`` if the local source
    is gone, ``StageError(S3_UPLOAD_FAILED, retryable=True)`` on any S3 error.
    Returns the key the handler stores implicitly via the deterministic layout.
    """
    track_id = track_id_for_job(ctx.job.id)
    key = source_key(track_id)
    bucket = ctx.settings.s3_media_bucket
    src = ctx.source_path
    if not src.exists():
        raise StageError(
            ErrorCode.SOURCE_MISSING,
            f"uploaded source not found on disk: {src.name}",
            retryable=False,
        )
    try:
        await asyncio.to_thread(_upload_source_blocking, ctx.settings, bucket, key, src)
    except StageError:
        raise
    except Exception as exc:  # boto3 / botocore failures
        raise StageError(
            ErrorCode.S3_UPLOAD_FAILED, f"source upload failed: {exc}"[:500], retryable=True
        ) from exc
    return key


def _upload_source_blocking(settings: Any, bucket: str, key: str, src: Path) -> None:
    s3 = s3_client(settings)
    size = src.stat().st_size
    head = _head_or_none(s3, bucket, key)
    if head is not None and int(head.get("ContentLength", -1)) == size:
        logger.info("source already uploaded (%s, %d bytes) — skipping", key, size)
        return
    s3.upload_file(str(src), bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    logger.info("source uploaded to s3://%s/%s (%d bytes)", bucket, key, size)


# --- WS5: cloud verifying + publishing ---------------------------------------


async def cloud_verifying(ctx: StageContext) -> JobStage:
    """Download the worker package and re-prove it; on success → publishing.

    ``download_package`` asserts handoff.json crossed the interface first; the
    existing ``load_and_verify_package`` re-proves every hash/size/format claim
    against the actual files. A missing handoff is transient because the worker
    may still be uploading; other intake failures require a re-split.
    """
    track_id = track_id_for_job(ctx.job.id)
    pkg_dir = ctx.job_dir / "package"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    prefix = separation_prefix(track_id)
    try:
        await asyncio.to_thread(
            download_package,
            s3_client(ctx.settings),
            ctx.settings.s3_media_bucket,
            prefix,
            pkg_dir,
        )
        pkg = await asyncio.to_thread(load_and_verify_package, pkg_dir)
    except PackageNotReady as exc:
        raise StageError(
            ErrorCode.CHECKSUM_MISMATCH,
            f"package verification failed: {exc}"[:500],
            retryable=True,
        ) from exc
    except IntakeError as exc:
        raise StageError(
            ErrorCode.CHECKSUM_MISMATCH,
            f"package verification failed: {exc}"[:500],
            retryable=False,
        ) from exc
    source_sha256 = pkg.handoff.get("source", {}).get("sha256")
    if ctx.job.input_checksum and source_sha256 != ctx.job.input_checksum:
        raise StageError(
            ErrorCode.CHECKSUM_MISMATCH,
            "worker package source checksum does not match the submitted source",
            retryable=False,
        )
    sep = pkg.handoff.get("separation", {})
    ctx.detail["package"] = {
        "duration": pkg.duration_seconds,
        "sample_count": sep.get("sample_count"),
    }
    return JobStage.publishing


async def cloud_publishing(ctx: StageContext) -> JobStage:
    """Transform the verified package + source into a browser generation.

    Source comes from the local job dir if present (re-runs), else is fetched
    from the private ``sources/{track_id}/source.mp4`` key. Then the fixed
    intake transform/stage/promote runs, and ``publish_track`` writes the tracks
    row and flips publishing → ready in one transaction. Successful publication
    removes the complete local job directory.
    """
    track_id = track_id_for_job(ctx.job.id)
    bucket = ctx.settings.s3_media_bucket

    try:
        s3 = s3_client(ctx.settings)
        source = ctx.source_path
        if not source.exists():
            source = ctx.job_dir / "source.mp4"
            await asyncio.to_thread(
                _download_source, s3, bucket, source_key(track_id), source
            )

        pkg = await asyncio.to_thread(load_and_verify_package, ctx.job_dir / "package")
        candidate = ctx.job_dir / "candidate"
        title = ctx.job.title or ctx.job.id.hex
        manifest = await asyncio.to_thread(transform, pkg, source, candidate, title, "")
        staged = await asyncio.to_thread(
            stage, s3, bucket, track_id, GENERATION, candidate, manifest
        )
        result = await Publisher(s3, bucket).publish_async(track_id, GENERATION, staged)
        integrity = dict(manifest.get("integrity") or {})
        if result.verification is not None:
            integrity["publisher"] = result.verification.to_integrity()
        await ctx.jobs.publish_track(
            ctx.job.id,
            title=manifest.get("title") or title,
            artist=manifest.get("artist", "") or "",
            duration_seconds=float(manifest.get("duration", 0.0) or 0.0),
            s3_prefix=result.s3_prefix,
            manifest_key=result.manifest_key,
            generation=GENERATION,
            integrity=integrity,
        )
    except StageError:
        raise
    except IntakeError as exc:
        raise StageError(
            ErrorCode.PUBLISH_FAILED, str(exc)[:500], retryable=False
        ) from exc
    except PublishError as exc:
        raise exc.to_stage_error() from exc
    except Exception as exc:
        raise StageError(
            ErrorCode.PUBLISH_FAILED, str(exc)[:500], retryable=True
        ) from exc
    shutil.rmtree(ctx.job_dir, ignore_errors=True)
    ctx.published = True
    return JobStage.ready


# --- small S3 helpers --------------------------------------------------------


def _head_or_none(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        head: dict[str, Any] = s3.head_object(Bucket=bucket, Key=key)
        return head
    except Exception as exc:
        if _is_not_found(exc):
            return None
        raise


def _download_source(s3: Any, bucket: str, key: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(bucket, key, str(dest))


def _is_not_found(exc: Exception) -> bool:
    resp = getattr(exc, "response", None)
    if not isinstance(resp, dict):
        return False
    code = str(resp.get("Error", {}).get("Code", ""))
    status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404

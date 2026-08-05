"""Publisher — verify staged worker output, promote it to an immutable generation.

Design spec sections 3/4, implementation plan Phase 3.4. This is the VPS-side
seam the orchestrator's `publishing` stage calls once the GPU worker has
finished and staged its output.

Contract
--------
The worker (``worker/handler.py``) uploads its output under

    tracks/{track_id}/{generation}/staging/
        video.mp4
        stems/{vocals,drums,bass,guitar,piano,other}.m4a
        multi-track.mp4        (optional)
        manifest.json

and reports every object back in its result payload as ``uploads[]`` records
of ``{file, key, sha256, size_bytes}`` (see ``worker/s3_ops.upload_outputs``).

The publisher then, in this exact order:

1. **Refuses to touch a published generation.** If ``manifest.json`` already
   exists at the destination prefix the generation is complete and immutable —
   the call is a no-op that returns ``already_published=True``. Generations are
   never overwritten (CloudFront caches them).
2. **Verifies** every reported object against what is actually in staging:
   size from ``head_object`` always, sha256 from the object's stored
   ``ChecksumSHA256`` when S3 has one, else by streaming the object (policy
   knob). Any missing object, size mismatch or digest mismatch raises
   :class:`ChecksumMismatch`, which maps to ``ErrorCode.CHECKSUM_MISMATCH``.
3. **Promotes** staging -> ``tracks/{track_id}/{generation}/`` with S3
   server-side copies. Nothing is ever downloaded and re-uploaded; objects over
   the single-copy limit go through multipart ``upload_part_copy``.
4. **Writes the manifest LAST.** Its presence at the generation prefix is the
   marker that the generation is complete; a publisher that dies mid-promotion
   leaves an incomplete prefix with no manifest, and the re-run redoes the
   copies (copies are idempotent — same source, same key).

Only then does the caller write the ``tracks`` row and flip the job to
``ready`` (see :func:`publish_job`, which delegates to
``JobRepository.publish_track`` — deterministic track id, so a crashed-and-rerun
publishing stage converges on the same row).

Note on checksum provenance
---------------------------
``worker/s3_ops.upload_file`` calls ``s3.upload_file`` without
``ChecksumAlgorithm="SHA256"``, so AWS does not store a SHA256 for staged
objects and :data:`ChecksumPolicy.AUTO` falls back to streaming them back to
the VPS to hash. That is correct but costs egress. Adding
``ExtraArgs={"ChecksumAlgorithm": "SHA256"}`` on the worker side would let the
publisher verify with ``head_object`` alone; the worker is out of this module's
lane, so the fallback is what ships.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import logging
import time
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .db.repository import JobRepository
from .errors import ErrorCode, StageError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .db.models import Job, Track

logger = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
STAGING_SEGMENT = "staging"

# CopyObject caps out at 5 GiB; larger objects need multipart copy.
SINGLE_COPY_LIMIT = 5 * 1024**3
COPY_PART_SIZE = 512 * 1024**2
_HASH_CHUNK = 8 * 1024 * 1024


# --- key layout ---------------------------------------------------------------


def generation_prefix(track_id: uuid.UUID | str, generation: int) -> str:
    """The immutable published prefix: ``tracks/{track_id}/{generation}/``."""
    return f"tracks/{track_id}/{generation}/"


def staging_prefix(track_id: uuid.UUID | str, generation: int) -> str:
    """Where the worker uploads before verification."""
    return f"{generation_prefix(track_id, generation)}{STAGING_SEGMENT}/"


def manifest_key(track_id: uuid.UUID | str, generation: int) -> str:
    """The object whose existence marks a generation complete."""
    return f"{generation_prefix(track_id, generation)}{MANIFEST_NAME}"


# --- errors -------------------------------------------------------------------


class PublishError(Exception):
    """Base publisher failure; every subclass carries a structured ErrorCode."""

    code: ErrorCode = ErrorCode.PUBLISH_FAILED
    retryable: bool = True

    def to_stage_error(self) -> StageError:
        """Convert to the orchestrator's stage-failure type."""
        return StageError(self.code, str(self)[:500], retryable=self.retryable)


class ChecksumMismatch(PublishError):
    """Staged bytes do not match what the worker reported.

    Not retryable: re-running the publisher against the same staged objects
    produces the same mismatch. The job must be re-split.
    """

    code = ErrorCode.CHECKSUM_MISMATCH
    retryable = False

    def __init__(self, message: str, failures: Sequence[ObjectVerification] = ()) -> None:
        super().__init__(message)
        self.failures = list(failures)


class StagedObjectMissing(ChecksumMismatch):
    """A reported object is not present in the staging prefix at all."""


class GenerationAlreadyPublished(PublishError):
    """The destination generation already has a manifest — it is immutable."""

    code = ErrorCode.PUBLISH_FAILED
    retryable = False


class PromotionFailed(PublishError):
    """A server-side copy failed."""

    code = ErrorCode.S3_UPLOAD_FAILED


class InvalidStemObject(PublishError):
    """A stem object is not library-grade (wrong format or absurd size).

    Not retryable: re-running the publisher against the same staged objects
    hits the same rejection. The job must be re-split / re-encoded.
    """

    code = ErrorCode.PUBLISH_FAILED
    retryable = False


# --- stem format guard --------------------------------------------------------

#: Stems must be compressed AAC in an .m4a container — never raw PCM. One
#: mistakenly-imported track with 6 × 95 MiB WAV stems ("The Pot", found
#: 2026-08-04) froze playback for good; this guard makes that class of object
#: impossible to publish or import again.
ALLOWED_STEM_SUFFIXES = (".m4a",)
#: Sane per-stem cap. A 320k AAC stem of a ~8 minute track is ~19 MiB; raw
#: WAV of the same track is ~95 MiB.
MAX_STEM_BYTES = 20 * 1024**2
_STEM_DIR = "stems/"


def validate_stem_object(file: str, size_bytes: int | None) -> str | None:
    """Return a rejection reason when a stem may not enter the library, else None.

    ``file`` is the manifest-relative path (e.g. ``stems/vocals.m4a``);
    non-stem objects (video, multitrack, manifest) are never rejected here.
    """
    if not file.startswith(_STEM_DIR):
        return None
    suffix = file[file.rfind(".") :].lower() if "." in file else ""
    if suffix not in ALLOWED_STEM_SUFFIXES:
        allowed = "/".join(ALLOWED_STEM_SUFFIXES)
        return f"stem format {suffix or '(none)'} not allowed (must be {allowed})"
    if size_bytes is not None and size_bytes > MAX_STEM_BYTES:
        return (
            f"stem is {size_bytes / 1024**2:.1f} MiB, over the "
            f"{MAX_STEM_BYTES / 1024**2:.0f} MiB cap (raw/uncompressed audio?)"
        )
    return None


def validate_stem_objects(objects: Iterable[tuple[str, int | None]]) -> None:
    """Raise :class:`InvalidStemObject` when any stem fails the format guard."""
    problems = [
        f"{file}: {reason}"
        for file, size in objects
        if (reason := validate_stem_object(file, size)) is not None
    ]
    if problems:
        raise InvalidStemObject(
            f"{len(problems)} stem object(s) rejected: " + "; ".join(problems[:6])
        )


# --- verification model -------------------------------------------------------


class ChecksumPolicy(StrEnum):
    """How hard the publisher works to prove sha256 equality."""

    #: Stored ChecksumSHA256 when S3 has one, otherwise stream the object.
    AUTO = "auto"
    #: Require a stored ChecksumSHA256; fail verification when absent.
    STORED = "stored"
    #: Always stream the object back and hash it.
    STREAM = "stream"
    #: Sizes only. Fast, and honestly recorded as unverified in the report.
    SIZE_ONLY = "size_only"


@dataclass(frozen=True)
class StagedObject:
    """One object the worker says it uploaded."""

    file: str  # relative key under the staging prefix, e.g. "stems/vocals.m4a"
    size_bytes: int
    sha256: str | None = None

    @classmethod
    def from_upload_record(cls, record: dict[str, Any]) -> StagedObject:
        """Build from a ``worker/s3_ops.upload_file`` record."""
        rel = record.get("file") or record.get("key", "")
        return cls(
            file=str(rel).lstrip("/"),
            size_bytes=int(record.get("size_bytes", -1)),
            sha256=record.get("sha256"),
        )

    @classmethod
    def from_result_payload(cls, payload: dict[str, Any]) -> list[StagedObject]:
        """Build the full expected set from a worker result payload."""
        return [cls.from_upload_record(r) for r in payload.get("uploads", [])]


@dataclass
class ObjectVerification:
    """Per-object outcome, kept whether it passed or failed (audit trail)."""

    file: str
    staging_key: str
    expected_size: int
    actual_size: int | None = None
    expected_sha256: str | None = None
    actual_sha256: str | None = None
    sha256_source: str = "unverified"  # s3-checksum | streamed | unverified
    ok: bool = False
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "size_bytes": self.actual_size,
            "sha256": self.actual_sha256 or self.expected_sha256,
            "sha256_source": self.sha256_source,
            "ok": self.ok,
            **({"reason": self.reason} if self.reason else {}),
        }


@dataclass
class VerificationReport:
    """What the publisher proved about the staged generation."""

    policy: ChecksumPolicy
    objects: list[ObjectVerification] = field(default_factory=list)
    extra_staged_keys: list[str] = field(default_factory=list)

    @property
    def failures(self) -> list[ObjectVerification]:
        return [o for o in self.objects if not o.ok]

    @property
    def ok(self) -> bool:
        return bool(self.objects) and not self.failures

    @property
    def total_bytes(self) -> int:
        return sum(o.actual_size or 0 for o in self.objects)

    @property
    def sha256_verified(self) -> int:
        return sum(1 for o in self.objects if o.sha256_source in ("s3-checksum", "streamed"))

    def to_integrity(self) -> dict[str, Any]:
        """The block stored on ``tracks.integrity`` for provenance."""
        return {
            "source": "publisher",
            "policy": self.policy.value,
            "object_count": len(self.objects),
            "total_bytes": self.total_bytes,
            "sha256_verified": self.sha256_verified,
            "sha256_unverified": len(self.objects) - self.sha256_verified,
            "objects": [o.as_dict() for o in self.objects],
            **({"extra_staged_keys": self.extra_staged_keys} if self.extra_staged_keys else {}),
        }


@dataclass
class PublishResult:
    track_id: str
    generation: int
    s3_prefix: str
    manifest_key: str
    copied_keys: list[str] = field(default_factory=list)
    bytes_copied: int = 0
    already_published: bool = False
    verification: VerificationReport | None = None
    elapsed_s: float = 0.0


# --- publisher ----------------------------------------------------------------


class Publisher:
    """Stateless over a boto3 S3 client; safe to construct per call.

    All methods here are blocking boto3 calls. Async callers use
    :meth:`publish_async` (or :func:`publish_job`), which offloads to a thread.
    """

    def __init__(
        self,
        s3: Any,
        bucket: str,
        *,
        checksum_policy: ChecksumPolicy = ChecksumPolicy.AUTO,
        single_copy_limit: int = SINGLE_COPY_LIMIT,
        copy_part_size: int = COPY_PART_SIZE,
    ) -> None:
        self.s3 = s3
        self.bucket = bucket
        self.checksum_policy = checksum_policy
        self.single_copy_limit = single_copy_limit
        self.copy_part_size = copy_part_size

    # --- low-level S3 helpers -------------------------------------------------

    def _head(self, key: str) -> dict[str, Any] | None:
        try:
            head: dict[str, Any] = self.s3.head_object(
                Bucket=self.bucket, Key=key, ChecksumMode="ENABLED"
            )
            return head
        except Exception as exc:  # botocore ClientError 404/403 or endpoint refusing the arg
            if _is_not_found(exc):
                return None
            # Some S3-compatible endpoints reject ChecksumMode; retry plain.
            try:
                plain: dict[str, Any] = self.s3.head_object(Bucket=self.bucket, Key=key)
                return plain
            except Exception as exc2:
                if _is_not_found(exc2):
                    return None
                raise

    def list_prefix(self, prefix: str) -> dict[str, int]:
        """Map relative key -> size for everything under ``prefix``."""
        found: dict[str, int] = {}
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                found[obj["Key"][len(prefix) :]] = obj["Size"]
        return found

    def _stream_sha256(self, key: str) -> str:
        body = self.s3.get_object(Bucket=self.bucket, Key=key)["Body"]
        digest = hashlib.sha256()
        try:
            for chunk in iter(lambda: body.read(_HASH_CHUNK), b""):
                digest.update(chunk)
        finally:
            close = getattr(body, "close", None)
            if close is not None:
                close()
        return digest.hexdigest()

    def copy_object(
        self, source_key: str, dest_key: str, size: int, content_type: str | None = None
    ) -> None:
        """Server-side copy. Never downloads; multipart above the copy limit.

        `content_type` overrides the source object's; leave it None to carry
        the source metadata through unchanged (what promotion wants — the
        worker already sets correct types). The legacy importer passes one to
        repair objects whose stored type is wrong.
        """
        if size > self.single_copy_limit:
            self._multipart_copy(source_key, dest_key, size, content_type)
            return
        extra: dict[str, Any] = {}
        if content_type is not None:
            extra = {"MetadataDirective": "REPLACE", "ContentType": content_type}
        self.s3.copy_object(
            Bucket=self.bucket,
            Key=dest_key,
            CopySource={"Bucket": self.bucket, "Key": source_key},
            **extra,
        )

    def _multipart_copy(
        self, source_key: str, dest_key: str, size: int, content_type: str | None = None
    ) -> None:
        create_args: dict[str, Any] = {"Bucket": self.bucket, "Key": dest_key}
        if content_type is not None:
            create_args["ContentType"] = content_type
        created = self.s3.create_multipart_upload(**create_args)
        upload_id = created["UploadId"]
        try:
            parts = []
            part_number = 1
            offset = 0
            while offset < size:
                last = min(offset + self.copy_part_size, size) - 1
                res = self.s3.upload_part_copy(
                    Bucket=self.bucket,
                    Key=dest_key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    CopySource={"Bucket": self.bucket, "Key": source_key},
                    CopySourceRange=f"bytes={offset}-{last}",
                )
                parts.append(
                    {"PartNumber": part_number, "ETag": res["CopyPartResult"]["ETag"]}
                )
                part_number += 1
                offset = last + 1
            self.s3.complete_multipart_upload(
                Bucket=self.bucket,
                Key=dest_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
        except Exception as exc:
            with contextlib.suppress(Exception):
                self.s3.abort_multipart_upload(
                    Bucket=self.bucket, Key=dest_key, UploadId=upload_id
                )
            raise PromotionFailed(f"multipart copy of {source_key} failed: {exc}") from exc

    # --- step 1: immutability guard ------------------------------------------

    def is_published(self, track_id: uuid.UUID | str, generation: int) -> bool:
        """True when the generation's manifest exists (generation is complete)."""
        return self._head(manifest_key(track_id, generation)) is not None

    # --- step 2: verification -------------------------------------------------

    def verify_staging(
        self,
        track_id: uuid.UUID | str,
        generation: int,
        reported: Iterable[StagedObject],
    ) -> VerificationReport:
        """Compare staged objects to the worker's report. Raises on mismatch."""
        prefix = staging_prefix(track_id, generation)
        staged = self.list_prefix(prefix)
        reported = list(reported)
        if not reported:
            raise ChecksumMismatch(
                f"worker reported no staged objects for {prefix} — nothing to verify"
            )

        report = VerificationReport(policy=self.checksum_policy)
        expected_files = {o.file for o in reported}
        report.extra_staged_keys = sorted(set(staged) - expected_files)

        for obj in reported:
            key = f"{prefix}{obj.file}"
            outcome = ObjectVerification(
                file=obj.file,
                staging_key=key,
                expected_size=obj.size_bytes,
                expected_sha256=obj.sha256,
            )
            report.objects.append(outcome)

            head = self._head(key)
            if head is None:
                outcome.reason = "missing from staging prefix"
                continue
            outcome.actual_size = int(head["ContentLength"])
            if obj.size_bytes >= 0 and outcome.actual_size != obj.size_bytes:
                outcome.reason = (
                    f"size mismatch: reported {obj.size_bytes}, staged {outcome.actual_size}"
                )
                continue

            actual, source = self._resolve_sha256(key, head)
            outcome.actual_sha256 = actual
            outcome.sha256_source = source
            if source == "unverified":
                if self.checksum_policy is ChecksumPolicy.STORED:
                    outcome.reason = "no stored ChecksumSHA256 and policy=stored"
                    continue
                outcome.ok = True
                continue
            if obj.sha256 and actual and actual.lower() != obj.sha256.lower():
                outcome.reason = (
                    f"sha256 mismatch: reported {obj.sha256[:12]}…, staged {actual[:12]}…"
                )
                continue
            outcome.ok = True

        failures = report.failures
        if failures:
            missing = [f for f in failures if f.reason == "missing from staging prefix"]
            summary = "; ".join(f"{f.file}: {f.reason}" for f in failures[:6])
            message = (
                f"staged verification failed for {len(failures)}/{len(report.objects)} "
                f"objects under {prefix}: {summary}"
            )
            if missing and len(missing) == len(failures):
                raise StagedObjectMissing(message, failures)
            raise ChecksumMismatch(message, failures)
        return report

    def _resolve_sha256(self, key: str, head: dict[str, Any]) -> tuple[str | None, str]:
        policy = self.checksum_policy
        if policy is ChecksumPolicy.SIZE_ONLY:
            return None, "unverified"
        stored = _stored_sha256(head)
        if stored is not None and policy is not ChecksumPolicy.STREAM:
            return stored, "s3-checksum"
        if policy in (ChecksumPolicy.AUTO, ChecksumPolicy.STREAM):
            return self._stream_sha256(key), "streamed"
        return None, "unverified"

    # --- step 3: promotion ----------------------------------------------------

    def promote(
        self,
        track_id: uuid.UUID | str,
        generation: int,
        report: VerificationReport,
    ) -> tuple[list[str], int]:
        """Copy staging -> generation prefix, manifest LAST. Returns (keys, bytes)."""
        src = staging_prefix(track_id, generation)
        dst = generation_prefix(track_id, generation)

        media = [o for o in report.objects if o.file != MANIFEST_NAME]
        manifests = [o for o in report.objects if o.file == MANIFEST_NAME]
        if not manifests:
            raise PublishError(
                f"staged set for {src} has no {MANIFEST_NAME} — refusing to publish"
            )

        copied: list[str] = []
        total = 0
        # Deterministic order keeps re-runs and logs comparable.
        for outcome in sorted(media, key=lambda o: o.file) + manifests:
            dest_key = f"{dst}{outcome.file}"
            try:
                self.copy_object(outcome.staging_key, dest_key, outcome.actual_size or 0)
            except PublishError:
                raise
            except Exception as exc:
                raise PromotionFailed(
                    f"copy {outcome.staging_key} -> {dest_key} failed: {exc}"
                ) from exc
            copied.append(dest_key)
            total += outcome.actual_size or 0
        return copied, total

    # --- full flow ------------------------------------------------------------

    def publish(
        self,
        track_id: uuid.UUID | str,
        generation: int,
        reported: Iterable[StagedObject],
    ) -> PublishResult:
        """Guard, verify, promote (manifest last). Blocking."""
        started = time.monotonic()
        prefix = generation_prefix(track_id, generation).rstrip("/")
        mkey = manifest_key(track_id, generation)

        if self.is_published(track_id, generation):
            logger.info("generation already published, no-op: %s", prefix)
            return PublishResult(
                track_id=str(track_id),
                generation=generation,
                s3_prefix=prefix,
                manifest_key=mkey,
                already_published=True,
                elapsed_s=round(time.monotonic() - started, 3),
            )

        reported = list(reported)
        # Format guard before anything else: a raw-PCM or absurdly large stem
        # must never be promoted into the library (see validate_stem_object).
        validate_stem_objects(
            (o.file, o.size_bytes if o.size_bytes >= 0 else None) for o in reported
        )

        report = self.verify_staging(track_id, generation, reported)
        copied, total = self.promote(track_id, generation, report)
        logger.info(
            "published %s: %d objects, %.1f MiB, sha256 verified %d/%d",
            prefix,
            len(copied),
            total / 1024**2,
            report.sha256_verified,
            len(report.objects),
        )
        return PublishResult(
            track_id=str(track_id),
            generation=generation,
            s3_prefix=prefix,
            manifest_key=mkey,
            copied_keys=copied,
            bytes_copied=total,
            verification=report,
            elapsed_s=round(time.monotonic() - started, 3),
        )

    async def publish_async(
        self,
        track_id: uuid.UUID | str,
        generation: int,
        reported: Iterable[StagedObject],
    ) -> PublishResult:
        return await asyncio.to_thread(self.publish, track_id, generation, list(reported))


# --- orchestrator entry point -------------------------------------------------


async def publish_job(
    *,
    jobs: JobRepository,
    publisher: Publisher,
    job: Job,
    worker_result: dict[str, Any],
    manifest: dict[str, Any],
    generation: int = 1,
) -> tuple[Track, PublishResult]:
    """Publish a finished job: promote objects, then write the tracks row.

    The track id is the deterministic one ``JobRepository.publish_track``
    derives from the job id, so S3 keys and the DB row agree on re-runs.
    Raises :class:`StageError` so the orchestrator loop's normal error handling
    applies.
    """
    from .db.repository import track_id_for_job

    track_id = track_id_for_job(job.id)
    reported = StagedObject.from_result_payload(worker_result)
    try:
        result = await publisher.publish_async(track_id, generation, reported)
    except PublishError as exc:
        raise exc.to_stage_error() from exc
    except Exception as exc:
        raise StageError(ErrorCode.PUBLISH_FAILED, str(exc)[:500]) from exc

    integrity: dict[str, Any] = {"worker": worker_result.get("integrity")}
    if result.verification is not None:
        integrity["publisher"] = result.verification.to_integrity()

    track = await jobs.publish_track(
        job.id,
        title=manifest.get("title") or job.title or job.id.hex,
        artist=manifest.get("artist", "") or "",
        duration_seconds=float(manifest.get("duration", 0.0) or 0.0),
        s3_prefix=result.s3_prefix,
        manifest_key=result.manifest_key,
        generation=generation,
        integrity=integrity,
    )
    return track, result


# --- small helpers ------------------------------------------------------------


def _stored_sha256(head: dict[str, Any]) -> str | None:
    """Return only a full-object AWS SHA-256, normalized from base64 to hex.

    Multipart uploads commonly expose ``ChecksumType=COMPOSITE`` and a value
    such as ``<base64>-11``. That checksum covers part digests, not the object
    bytes, and cannot be compared to the worker's ordinary file SHA-256.
    ``AUTO`` therefore streams the object when this helper returns ``None``.
    """
    if str(head.get("ChecksumType", "")).upper() == "COMPOSITE":
        return None
    raw = head.get("ChecksumSHA256")
    if not raw:
        return None
    try:
        return base64.b64decode(raw, validate=True).hex()
    except (binascii.Error, ValueError):
        return str(raw).lower()


def _is_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = str(response.get("Error", {}).get("Code", ""))
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("404", "NoSuchKey", "NotFound") or status == 404

"""Completed delivery intake. No source acquisition, separation, or conversion.

Clients can write only checksum-bound intake objects outside tracks/. Publication
uses server-owned staging and a locked import row, fencing stale worker claims.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import math
import re
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any, TypeVar

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..api.media import s3_client
from ..db.models import CompletedImport, CompletedImportEvent, Track, utcnow
from ..db.repository import track_id_for_import, track_location_problem
from ..settings import Settings
from .audio_quality import build_six_stem_mix_filter, parse_mix_quality
from .delivery_profile import CANONICAL_STEM_IDS, STEM_INTER_DURATION_TOLERANCE_SEC
from .lossless_intake import TRACK_BUDGET_BPS, IntakeError, audit_candidate, stage
from .publisher import Publisher, generation_prefix, manifest_key

T = TypeVar("T")

MAX_MANIFEST_BYTES = 1024 * 1024
FILES = ("video.mp4", *(f"stems/{role}.m4a" for role in CANONICAL_STEM_IDS))


class InvalidCandidate(ValueError):
    """Safe, fixed error text only; never include uploaded content or URLs."""


def canonical(manifest: dict[str, Any]) -> bytes:
    return json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def validate_manifest(manifest: dict[str, Any]) -> str:
    try:
        encoded = canonical(manifest)
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise ValueError()
        if manifest["version"] != 3 or manifest["delivery_profile"] != "shizzle-browser-v1":
            raise ValueError()
        duration = manifest["duration"]
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not 0 < duration <= 1800
        ):
            raise ValueError()
        for key in ("title", "artist"):
            if not isinstance(manifest[key], str) or not 0 < len(manifest[key]) <= 1024:
                raise ValueError()
        stems = manifest["stems"]
        if len(stems) != 6 or {s["id"] for s in stems} != set(CANONICAL_STEM_IDS):
            raise ValueError()
        gains = [s["default_gain_db"] for s in stems]
        if (
            any(
                isinstance(g, bool)
                or not isinstance(g, (int, float))
                or not math.isfinite(g)
                or g > 0
                for g in gains
            )
            or len(set(gains)) != 1
        ):
            raise ValueError()
        if manifest["video"] != "video.mp4" or any(
            s["file"] != f"stems/{s['id']}.m4a" for s in stems
        ):
            raise ValueError()
        objects = manifest["integrity"]["objects"]
        if len(objects) != 7 or {o["file"] for o in objects} != set(FILES):
            raise ValueError()
        for obj in objects:
            size = obj["bytes"]
            cap = (128 if obj["file"] == "video.mp4" else 64) * 1024**2
            if (
                type(size) is not int
                or not 0 < size <= cap
                or not re.fullmatch(r"[0-9a-f]{64}", obj["sha256"])
            ):
                raise ValueError()
        if sum(o["bytes"] for o in objects) * 8 / duration > TRACK_BUDGET_BPS:
            raise ValueError()
        timeline = manifest["timeline"]
        if timeline["start_ms"] != 0 or abs(timeline["duration_ms"] / 1000 - duration) > 0.001:
            raise ValueError()
        return hashlib.sha256(encoded).hexdigest()
    except (KeyError, TypeError, ValueError, OverflowError):
        raise InvalidCandidate("INVALID_MANIFEST") from None


def track_id(item: CompletedImport) -> uuid.UUID:
    return track_id_for_import("completed:" + item.digest)


def input_key(item: CompletedImport, file: str) -> str:
    return f"imports/{item.id}/input/{file}"


def event(session: AsyncSession, item: CompletedImport, status: str) -> None:
    item.status = status
    session.add(CompletedImportEvent(import_id=item.id, event=status))


class ImportRepository:
    def __init__(self, sf: async_sessionmaker[AsyncSession]) -> None:
        self.sf = sf

    async def create(self, manifest: dict[str, Any]) -> CompletedImport:
        digest = validate_manifest(manifest)
        identifier = uuid.uuid5(uuid.NAMESPACE_URL, "shizzle:completed:" + digest)
        try:
            async with self.sf() as session, session.begin():
                item = await session.get(CompletedImport, identifier)
                if item is None:
                    item = CompletedImport(
                        id=identifier, digest=digest, manifest=manifest, attempt=0
                    )
                    session.add(item)
                    event(session, item, "uploading")
        except IntegrityError:
            # Another create committed the same content-addressed contribution.
            pass
        result = await self.get(identifier)
        if result is None:
            raise RuntimeError("Contribution creation did not persist")
        return result

    async def get(self, identifier: uuid.UUID) -> CompletedImport | None:
        async with self.sf() as session:
            return await session.get(CompletedImport, identifier)

    async def finalize(self, identifier: uuid.UUID) -> CompletedImport | None:
        async with self.sf() as session, session.begin():
            item = await session.get(CompletedImport, identifier, with_for_update=True)
            if item and item.status == "uploading":
                event(session, item, "validating")
        return await self.get(identifier)

    async def claim(self, owner: str, seconds: float) -> CompletedImport | None:
        now = utcnow()
        async with self.sf() as session, session.begin():
            item = await session.scalar(
                select(CompletedImport)
                .where(
                    CompletedImport.status.in_(["validating", "publishing"]),
                    or_(
                        CompletedImport.lease_expires_at.is_(None),
                        CompletedImport.lease_expires_at < now,
                    ),
                    or_(
                        CompletedImport.next_retry_at.is_(None),
                        CompletedImport.next_retry_at <= now,
                    ),
                )
                .order_by(CompletedImport.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if item is None:
                return None
            item.lease_owner = owner
            item.lease_expires_at = now + timedelta(seconds=seconds)
            item.attempt += 1
            return item

    async def renew(self, identifier: uuid.UUID, owner: str, seconds: float) -> None:
        async with self.sf() as session, session.begin():
            await session.execute(
                update(CompletedImport)
                .where(
                    CompletedImport.id == identifier,
                    CompletedImport.lease_owner == owner,
                )
                .values(lease_expires_at=utcnow() + timedelta(seconds=seconds))
            )

    async def failure(self, identifier: uuid.UUID, owner: str, code: str, retry: bool) -> None:
        async with self.sf() as session, session.begin():
            item = await session.get(CompletedImport, identifier, with_for_update=True)
            if item is None or item.lease_owner != owner or item.status == "ready":
                return
            item.error_code = code
            item.lease_owner = None
            item.lease_expires_at = None
            if retry:
                item.next_retry_at = utcnow() + timedelta(
                    seconds=min(300, 2 ** min(item.attempt, 8))
                )
                session.add(CompletedImportEvent(import_id=item.id, event="retry_scheduled"))
            else:
                event(session, item, "failed")


def upload_instructions(item: CompletedImport, s3: Any, bucket: str) -> list[dict[str, Any]]:
    if item.status != "uploading":
        return []
    result = []
    for obj in item.manifest["integrity"]["objects"]:
        checksum = base64.b64encode(bytes.fromhex(obj["sha256"])).decode()
        params = {
            "Bucket": bucket,
            "Key": input_key(item, obj["file"]),
            "ChecksumSHA256": checksum,
            "ContentLength": obj["bytes"],
        }
        present = False
        try:
            head = s3.head_object(Bucket=bucket, Key=params["Key"], ChecksumMode="ENABLED")
            present = (
                head.get("ChecksumSHA256") == checksum and head["ContentLength"] == obj["bytes"]
            )
            if not head.get("ChecksumSHA256") and head["ContentLength"] == obj["bytes"]:
                # Older S3 clients/emulators may omit the checksum on HEAD.
                body = s3.get_object(Bucket=bucket, Key=params["Key"])["Body"]
                try:
                    digest = hashlib.sha256()
                    size = 0
                    while chunk := body.read(1024 * 1024):
                        size += len(chunk)
                        if size > obj["bytes"]:
                            break
                        digest.update(chunk)
                    present = size == obj["bytes"] and digest.hexdigest() == obj["sha256"]
                finally:
                    body.close()
        except s3.exceptions.ClientError as exc:
            if exc.response["Error"]["Code"] not in ("404", "NoSuchKey", "NotFound"):
                raise
        if not present:
            result.append(
                {
                    "file": obj["file"],
                    "bytes": obj["bytes"],
                    "sha256": obj["sha256"],
                    "url": s3.generate_presigned_url("put_object", Params=params, ExpiresIn=900),
                    "headers": {
                        "x-amz-checksum-sha256": checksum,
                        "Content-Length": str(obj["bytes"]),
                    },
                }
            )
    return result


def download_candidate(item: CompletedImport, s3: Any, bucket: str, directory: Path) -> None:
    for obj in item.manifest["integrity"]["objects"]:
        target = directory / obj["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        size = 0
        body = s3.get_object(Bucket=bucket, Key=input_key(item, obj["file"]))["Body"]
        try:
            with target.open("wb") as output:
                while chunk := body.read(1024 * 1024):
                    size += len(chunk)
                    if size > obj["bytes"]:
                        raise InvalidCandidate("INTEGRITY_FAILED")
                    digest.update(chunk)
                    output.write(chunk)
        finally:
            body.close()
        if size != obj["bytes"] or digest.hexdigest() != obj["sha256"]:
            raise InvalidCandidate("INTEGRITY_FAILED")


def validate_candidate(item: CompletedImport, directory: Path) -> dict[str, Any]:
    try:
        audits = audit_candidate(directory, item.manifest["duration"])
        durations = [
            float(a["probe"]["streams"][0]["duration"])
            for a in audits
            if a["artifact"].startswith("stems/")
        ]
        if max(durations) - min(durations) > STEM_INTER_DURATION_TOLERANCE_SEC:
            raise InvalidCandidate("TIMELINE_FAILED")
        stems = {s["id"]: s for s in item.manifest["stems"]}
        command = ["ffmpeg", "-nostdin", "-hide_banner", "-nostats"]
        for role in CANONICAL_STEM_IDS:
            command += ["-i", str(directory / f"stems/{role}.m4a")]
        command += [
            "-filter_complex",
            build_six_stem_mix_filter([stems[r]["default_gain_db"] for r in CANONICAL_STEM_IDS]),
            "-map",
            "[out_ebu]",
            "-map",
            "[out_stats]",
            "-f",
            "null",
            "-",
        ]
        measured = subprocess.run(command, capture_output=True, text=True, timeout=1800, check=True)
        quality = parse_mix_quality(measured.stderr)
        if not quality.passed_pre_limiter:
            raise InvalidCandidate("AUDIO_QUALITY_FAILED")
        return {"objects": audits, "default_mix": quality.as_dict()}
    except (IntakeError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        if isinstance(exc, InvalidCandidate):
            raise
        raise InvalidCandidate("MEDIA_VALIDATION_FAILED") from None


def publish_candidate(item: CompletedImport, directory: Path, s3: Any, bucket: str) -> None:
    tid = track_id(item)
    publisher = Publisher(s3, bucket)
    if publisher.is_published(tid, 1):
        body = s3.get_object(Bucket=bucket, Key=manifest_key(tid, 1))["Body"]
        try:
            existing = json.loads(body.read(MAX_MANIFEST_BYTES + 1))
        finally:
            body.close()
        if canonical(existing) != canonical(item.manifest):
            raise InvalidCandidate("PUBLISHED_GENERATION_CONFLICT")
        return
    reported = stage(s3, bucket, tid, 1, directory, item.manifest)
    publisher.publish(tid, 1, reported)


async def blocking(function: Callable[..., T], *args: Any) -> T:
    # Cancellation must not release a publication lock or delete a workspace
    # while its underlying thread can still write media.
    work = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await work
        raise


async def process_one(repo: ImportRepository, settings: Settings, owner: str) -> bool:
    seconds = max(30, settings.orchestrator_lease_seconds)
    item = await repo.claim(owner, seconds)
    if item is None:
        return False

    async def renew() -> None:
        while True:
            await asyncio.sleep(seconds / 3)
            await repo.renew(item.id, owner, seconds)

    renewal = asyncio.create_task(renew())
    try:
        s3 = s3_client(settings)
        with tempfile.TemporaryDirectory(prefix="completed-", dir=settings.data_dir) as temporary:
            directory = Path(temporary)
            await blocking(download_candidate, item, s3, settings.s3_media_bucket, directory)
            validation = await blocking(validate_candidate, item, directory)
            # Hold the row lock over S3 promotion and DB registration. A stale
            # claimant cannot promote concurrently even if its lease expired.
            async with repo.sf() as session, session.begin():
                current = await session.get(CompletedImport, item.id, with_for_update=True)
                if current is None or current.lease_owner != owner or current.status == "ready":
                    return True
                tid = track_id(current)
                track = await session.get(Track, tid, with_for_update=True)
                if track and (
                    track.deleted_at is not None
                    or track.generation != 1
                    or track.s3_prefix != generation_prefix(tid, 1).rstrip("/")
                    or track.manifest_key != manifest_key(tid, 1)
                ):
                    raise InvalidCandidate("TRACK_CONFLICT")
                current.validation = validation
                event(session, current, "publishing")
                await blocking(publish_candidate, current, directory, s3, settings.s3_media_bucket)
                prefix = generation_prefix(tid, 1).rstrip("/")
                key = manifest_key(tid, 1)
                if track_location_problem(prefix, key):
                    raise InvalidCandidate("PUBLISHED_LOCATION_INVALID")
                if track is None:
                    manifest = current.manifest
                    session.add(
                        Track(
                            id=tid,
                            title=manifest["title"],
                            artist=manifest["artist"],
                            duration_seconds=manifest["duration"],
                            generation=1,
                            s3_prefix=prefix,
                            manifest_key=key,
                            integrity=manifest["integrity"],
                        )
                    )
                event(session, current, "ready")
                current.error_code = None
                current.lease_owner = None
                current.lease_expires_at = None
    except InvalidCandidate as exc:
        await repo.failure(item.id, owner, str(exc), False)
    except Exception:
        # Never persist raw S3/HTTP exceptions (may contain credentials).
        await repo.failure(item.id, owner, "PUBLICATION_RETRY", True)
    finally:
        renewal.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewal
    return True

"""Typed repository layer — the only place SQL happens.

Routes and the orchestrator call these methods; sessions never leak out.
Every public method is its own transaction against the injected session
factory, so callers cannot half-commit orchestration state.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..errors import ErrorCode
from .models import (
    ALLOWED_TRANSITIONS,
    RUNNABLE_STAGES,
    TERMINAL_STAGES,
    Job,
    JobEvent,
    JobStage,
    OrchestratorHeartbeat,
    SourceType,
    Track,
    utcnow,
)

# Namespace for deriving a deterministic track id from a job id, so a crashed
# and re-run publishing stage converges on the same row (idempotency).
_TRACK_NS = uuid.UUID("6f6b3c1e-9a34-4c86-a0f5-1d2ff0a15a55")


def track_id_for_job(job_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_TRACK_NS, f"track:{job_id}")


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; normalize to UTC-aware for comparisons."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


class InvalidTransition(Exception):
    def __init__(self, job_id: uuid.UUID, current: JobStage, target: JobStage) -> None:
        super().__init__(f"job {job_id}: illegal transition {current} -> {target}")
        self.current = current
        self.target = target


class JobRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # --- creation / reads ----------------------------------------------------

    async def create_job(
        self,
        *,
        source_type: SourceType,
        source_ref: str,
        title: str | None = None,
        idempotency_key: str | None = None,
        profile_version: int = 1,
        input_checksum: str | None = None,
        job_id: uuid.UUID | None = None,
    ) -> Job:
        job = Job(
            id=job_id or uuid.uuid4(),
            source_type=source_type,
            source_ref=source_ref,
            title=title,
            status=JobStage.pending,
            idempotency_key=idempotency_key or uuid.uuid4().hex,
            profile_version=profile_version,
            input_checksum=input_checksum,
        )
        async with self._sf() as session, session.begin():
            session.add(job)
            session.add(
                JobEvent(
                    job_id=job.id,
                    event="created",
                    detail={"source_type": source_type.value, "source_ref": source_ref},
                )
            )
        return job

    async def get_job(self, job_id: uuid.UUID) -> Job | None:
        async with self._sf() as session:
            return await session.get(Job, job_id)

    async def get_job_by_idempotency_key(self, key: str) -> Job | None:
        async with self._sf() as session:
            res = await session.execute(select(Job).where(Job.idempotency_key == key))
            return res.scalar_one_or_none()

    async def list_jobs(self, *, limit: int = 50, offset: int = 0) -> tuple[list[Job], int]:
        async with self._sf() as session:
            total = (await session.execute(select(func.count(Job.id)))).scalar_one()
            res = await session.execute(
                select(Job).order_by(Job.created_at.desc(), Job.id).limit(limit).offset(offset)
            )
            return list(res.scalars().all()), total

    async def list_events(self, job_id: uuid.UUID) -> list[JobEvent]:
        async with self._sf() as session:
            res = await session.execute(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
            )
            return list(res.scalars().all())

    async def append_event(
        self, job_id: uuid.UUID, event: str, detail: dict[str, Any] | None = None
    ) -> None:
        async with self._sf() as session, session.begin():
            session.add(JobEvent(job_id=job_id, event=event, detail=detail))

    # --- lease loop ----------------------------------------------------------

    async def claim_next(
        self, *, worker_id: str, lease_seconds: float, now: datetime | None = None
    ) -> Job | None:
        """Claim one runnable job with SELECT ... FOR UPDATE SKIP LOCKED.

        Runnable = status in RUNNABLE_STAGES, retry due (next_retry_at null or
        past), and lease free or expired. An expired foreign lease is
        reclaimed and recorded as a `lease_reclaimed` event.
        """
        now = now or utcnow()
        async with self._sf() as session, session.begin():
            stmt = (
                select(Job)
                .where(
                    Job.status.in_(RUNNABLE_STAGES),
                    or_(Job.next_retry_at.is_(None), Job.next_retry_at <= now),
                    or_(Job.lease_expires_at.is_(None), Job.lease_expires_at <= now),
                )
                .order_by(Job.created_at, Job.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            job = (await session.execute(stmt)).scalar_one_or_none()
            if job is None:
                return None
            reclaimed_from = None
            if job.lease_owner is not None and job.lease_owner != worker_id:
                reclaimed_from = job.lease_owner
            job.lease_owner = worker_id
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.updated_at = now
            if reclaimed_from is not None:
                session.add(
                    JobEvent(
                        job_id=job.id,
                        event="lease_reclaimed",
                        detail={"from": reclaimed_from, "by": worker_id, "stage": job.status.value},
                    )
                )
            return job

    async def renew_lease(
        self, job_id: uuid.UUID, *, worker_id: str, lease_seconds: float
    ) -> bool:
        """Extend the lease; returns False if this worker no longer owns it."""
        now = utcnow()
        async with self._sf() as session, session.begin():
            res = await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.lease_owner == worker_id)
                .values(lease_expires_at=now + timedelta(seconds=lease_seconds), updated_at=now)
            )
            return res.rowcount == 1

    async def release_lease(self, job_id: uuid.UUID, *, worker_id: str) -> None:
        async with self._sf() as session, session.begin():
            await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.lease_owner == worker_id)
                .values(lease_owner=None, lease_expires_at=None, updated_at=utcnow())
            )

    # --- state machine -------------------------------------------------------

    async def advance(
        self,
        job_id: uuid.UUID,
        *,
        from_stage: JobStage,
        to_stage: JobStage,
        duration_s: float | None = None,
        detail: dict[str, Any] | None = None,
        runpod_job_id: str | None = None,
        output_checksums: dict[str, Any] | None = None,
    ) -> Job:
        """Move a job one legal step forward; records timing and an event.

        Raises InvalidTransition when the row is not in `from_stage` anymore
        (e.g. a duplicate completion from a stale worker) — callers treat that
        as a no-op signal, and no state is modified.
        """
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise InvalidTransition(job_id, JobStage.failed, to_stage)
            if job.status != from_stage or to_stage not in ALLOWED_TRANSITIONS[job.status]:
                raise InvalidTransition(job_id, job.status, to_stage)
            job.status = to_stage
            job.next_retry_at = None
            job.error_code = None
            job.error_detail = None
            if runpod_job_id is not None:
                job.runpod_job_id = runpod_job_id
            if output_checksums is not None:
                job.output_checksums = output_checksums
            if duration_s is not None:
                timings = dict(job.stage_timings or {})
                timings[from_stage.value] = round(
                    float(timings.get(from_stage.value, 0.0)) + duration_s, 3
                )
                job.stage_timings = timings
            job.updated_at = utcnow()
            session.add(
                JobEvent(
                    job_id=job.id,
                    event="stage_completed",
                    detail={
                        "from": from_stage.value,
                        "to": to_stage.value,
                        "duration_s": duration_s,
                        **(detail or {}),
                    },
                )
            )
            return job

    async def schedule_retry(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        error_code: ErrorCode,
        error_detail: str,
        retry_in_seconds: float,
    ) -> Job:
        """Record a retryable failure: attempt += 1, next_retry_at set, lease freed."""
        now = utcnow()
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None or job.status in TERMINAL_STAGES:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.failed)
            job.attempt += 1
            job.next_retry_at = now + timedelta(seconds=retry_in_seconds)
            job.error_code = error_code.value
            job.error_detail = error_detail
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            session.add(
                JobEvent(
                    job_id=job.id,
                    event="retry_scheduled",
                    detail={
                        "stage": job.status.value,
                        "attempt": job.attempt,
                        "error_code": error_code.value,
                        "error_detail": error_detail[:500],
                        "retry_in_seconds": retry_in_seconds,
                        "worker": worker_id,
                    },
                )
            )
            return job

    async def fail_job(
        self,
        job_id: uuid.UUID,
        *,
        error_code: ErrorCode,
        error_detail: str,
        worker_id: str | None = None,
    ) -> Job:
        """Terminal failure from any non-terminal stage; idempotent on re-call."""
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.failed)
            if job.status == JobStage.failed:
                return job  # duplicate failure report — no-op
            if job.status == JobStage.ready:
                raise InvalidTransition(job_id, job.status, JobStage.failed)
            from_stage = job.status
            job.status = JobStage.failed
            job.error_code = error_code.value
            job.error_detail = error_detail
            job.lease_owner = None
            job.lease_expires_at = None
            job.next_retry_at = None
            job.updated_at = utcnow()
            session.add(
                JobEvent(
                    job_id=job.id,
                    event="failed",
                    detail={
                        "stage": from_stage.value,
                        "error_code": error_code.value,
                        "error_detail": error_detail[:500],
                        "worker": worker_id,
                    },
                )
            )
            return job

    # --- publishing ----------------------------------------------------------

    async def publish_track(
        self,
        job_id: uuid.UUID,
        *,
        title: str,
        artist: str = "",
        duration_seconds: float,
        s3_prefix: str,
        manifest_key: str,
        generation: int = 1,
        integrity: dict[str, Any] | None = None,
        duration_stage_s: float | None = None,
    ) -> Track:
        """Create the track row and move publishing -> ready, in one transaction.

        Idempotent: the track id is derived deterministically from the job id,
        so a crashed-and-rerun publishing stage (or a duplicate completion
        event) converges on the same row and does not double-publish.
        """
        tid = track_id_for_job(job_id)
        try:
            return await self._publish_track_once(
                job_id,
                tid,
                title=title,
                artist=artist,
                duration_seconds=duration_seconds,
                s3_prefix=s3_prefix,
                manifest_key=manifest_key,
                generation=generation,
                integrity=integrity,
                duration_stage_s=duration_stage_s,
            )
        except IntegrityError:
            # A concurrent publisher won the insert race; converge on its row.
            async with self._sf() as session:
                won = await session.get(Track, tid)
                if won is not None:
                    return won
            raise

    async def _publish_track_once(
        self,
        job_id: uuid.UUID,
        tid: uuid.UUID,
        *,
        title: str,
        artist: str,
        duration_seconds: float,
        s3_prefix: str,
        manifest_key: str,
        generation: int,
        integrity: dict[str, Any] | None,
        duration_stage_s: float | None,
    ) -> Track:
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.ready)

            existing = await session.get(Track, tid)
            if existing is not None and job.status == JobStage.ready:
                # Duplicate completion — full no-op.
                session.add(
                    JobEvent(
                        job_id=job_id,
                        event="duplicate_completion_ignored",
                        detail={"track_id": str(tid)},
                    )
                )
                return existing

            if job.status != JobStage.publishing:
                raise InvalidTransition(job_id, job.status, JobStage.ready)

            if existing is None:
                track = Track(
                    id=tid,
                    title=title,
                    artist=artist,
                    duration_seconds=duration_seconds,
                    s3_prefix=s3_prefix,
                    generation=generation,
                    manifest_key=manifest_key,
                    integrity=integrity,
                )
                session.add(track)
                # Flush the INSERT before touching jobs.track_id: without a
                # relationship() the unit of work does not know jobs -> tracks
                # ordering, and Postgres enforces the FK (SQLite hid this).
                await session.flush()
            else:
                track = existing

            job.track_id = tid
            job.status = JobStage.ready
            job.next_retry_at = None
            job.error_code = None
            job.error_detail = None
            job.lease_owner = None
            job.lease_expires_at = None
            if duration_stage_s is not None:
                timings = dict(job.stage_timings or {})
                timings[JobStage.publishing.value] = round(duration_stage_s, 3)
                job.stage_timings = timings
            job.updated_at = utcnow()
            session.add(
                JobEvent(
                    job_id=job_id,
                    event="track_published",
                    detail={"track_id": str(tid), "s3_prefix": s3_prefix},
                )
            )
            return track


class TrackRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get(self, track_id: uuid.UUID) -> Track | None:
        async with self._sf() as session:
            return await session.get(Track, track_id)

    async def list_tracks(self, *, include_deleted: bool = False) -> list[Track]:
        async with self._sf() as session:
            stmt = select(Track).order_by(Track.created_at.desc(), Track.id)
            if not include_deleted:
                stmt = stmt.where(Track.deleted_at.is_(None))
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def soft_delete(self, track_id: uuid.UUID) -> bool:
        """Mark deleted (design: no silent retention deletion; manual only)."""
        async with self._sf() as session, session.begin():
            track = await session.get(Track, track_id, with_for_update=True)
            if track is None or track.deleted_at is not None:
                return False
            track.deleted_at = utcnow()
            return True


class HeartbeatRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def beat(self, worker_id: str) -> None:
        async with self._sf() as session, session.begin():
            row = await session.get(OrchestratorHeartbeat, worker_id)
            if row is None:
                session.add(OrchestratorHeartbeat(worker_id=worker_id, updated_at=utcnow()))
            else:
                row.updated_at = utcnow()

    async def latest(self) -> datetime | None:
        async with self._sf() as session:
            res = await session.execute(select(func.max(OrchestratorHeartbeat.updated_at)))
            return _aware(res.scalar_one_or_none())

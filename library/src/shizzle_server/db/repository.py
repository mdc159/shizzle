"""Typed repository layer — the only place SQL happens.

Routes and the orchestrator call these methods; sessions never leak out.
Every public method is its own transaction against the injected session
factory, so callers cannot half-commit orchestration state.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
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
    PlaybackEvent,
    PlaybackSession,
    SourceType,
    Track,
    TrackGenerationEvent,
    utcnow,
)

# Namespace for deriving a deterministic track id from a job id, so a crashed
# and re-run publishing stage converges on the same row (idempotency).
_TRACK_NS = uuid.UUID("6f6b3c1e-9a34-4c86-a0f5-1d2ff0a15a55")

PLAYBACK_INCIDENT_EVENTS = frozenset(
    {
        "play-rejected",
        "stem-media-error",
        "stem-clock-stalled",
        "video-media-error",
        "video-buffering",
        "video-clock-stalled",
        "audio-context-not-running",
        "render-silence",
        "recovery-failed",
        "fatal",
    }
)

_DISPATCH_STARTED_EVENT = "runpod_dispatch_started"
_DISPATCH_CONFIRMED_EVENT = "runpod_dispatched"
_LEGACY_DISPATCH_UNCONFIRMED_EVENT = "dispatch_unconfirmed"


def pending_runpod_dispatch(events: Iterable[JobEvent]) -> JobEvent | None:
    """Return the latest dispatch that may have been accepted but is unconfirmed.

    ``dispatch_unconfirmed`` predates durable reservations. It must remain
    fail-closed during a rolling upgrade; otherwise an in-flight legacy job
    could be submitted again by the first process running the new code.
    """
    pending: JobEvent | None = None
    pending_key: str | None = None
    for event in events:
        detail = event.detail if isinstance(event.detail, dict) else {}
        event_key = detail.get("idempotency_key")
        if event.event == _LEGACY_DISPATCH_UNCONFIRMED_EVENT:
            pending = event
            pending_key = None
        elif event.event == _DISPATCH_STARTED_EVENT and isinstance(event_key, str):
            pending = event
            pending_key = event_key
        elif (
            event.event == _DISPATCH_CONFIRMED_EVENT
            and pending is not None
            and (
                (pending_key is None and event_key is None)
                or event_key == pending_key
            )
        ):
            pending = None
            pending_key = None
    return pending


def track_id_for_job(job_id: uuid.UUID) -> uuid.UUID:
    return uuid.uuid5(_TRACK_NS, f"track:{job_id}")


class PublishRefusedError(ValueError):
    """A track row was refused because its storage location is not a real
    published object prefix (invariant C7).

    The 2026-08-19 phantom track proved a stub pipeline could publish a
    ``local/...`` row with no object in S3; this guard makes that shape
    impossible at the single point where a job creates its track row.
    """


def track_location_problem(s3_prefix: str, manifest_key: str) -> str | None:
    """Return why a track location is not a real published object, or None.

    Invariant C7: published tracks live under ``tracks/`` with the manifest
    written last as ``.../manifest.json`` (C2). Anything else — the Phase 2
    ``local/`` placeholder, an empty prefix — can never be a playable track.
    """
    problems = []
    body = s3_prefix.removeprefix("tracks/")
    if not s3_prefix.startswith("tracks/") or not body.strip("/"):
        problems.append(f"s3_prefix {s3_prefix!r} is not under tracks/ with a non-empty path")
    elif body != body.strip("/") or "//" in body:
        # C7 pins the manifest key to "<s3_prefix>/manifest.json" verbatim: a
        # prefix with empty segments or a trailing slash could only pass by
        # tolerating a location that is not its own manifest's real parent.
        problems.append(f"s3_prefix {s3_prefix!r} is not a canonical tracks/ path")
    expected_manifest = f"{s3_prefix}/manifest.json"
    if manifest_key != expected_manifest:
        problems.append(
            f"manifest_key {manifest_key!r} is not the prefix manifest {expected_manifest!r}"
        )
    return "; ".join(problems) or None


def track_id_for_import(source_ref: str) -> uuid.UUID:
    """Deterministic id for a track with no job behind it (library import).

    `source_ref` is the stable identifier of the imported material — for the
    legacy S3 library, the `karaoke/pub/{folder}` folder name. Re-running an
    import converges on the same row instead of duplicating the library.
    """
    return uuid.uuid5(_TRACK_NS, f"import:{source_ref}")


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


def _require_lease(job: Job, worker_id: str, target: JobStage, now: datetime) -> None:
    """B2 fence for stage-outcome writes: inside the locked transaction, the
    caller must still own an unexpired lease, exactly like record_dispatch."""
    lease_expires_at = _aware(job.lease_expires_at)
    if (
        job.lease_owner != worker_id
        or lease_expires_at is None
        or lease_expires_at <= now
    ):
        raise InvalidTransition(job.id, job.status, target)


class TrackGenerationConflict(Exception):
    """The active generation changed after a migration read its baseline."""

    def __init__(self, track_id: uuid.UUID, expected: int, actual: int) -> None:
        super().__init__(f"track {track_id}: expected active generation {expected}, found {actual}")
        self.track_id = track_id
        self.expected = expected
        self.actual = actual


class ImportConflict(Exception):
    """An import would reset a migrated generation or resurrect a deleted track.

    Only `activate_generation` (the C5 compare-and-swap path) may move the
    active-generation pointer; restores of deleted tracks are explicit ops.
    """

    def __init__(self, track_id: uuid.UUID, generation: int, actual_generation: int,
                 deleted: bool) -> None:
        reasons = []
        if actual_generation != generation:
            reasons.append(f"active generation {actual_generation} != import generation "
                           f"{generation}")
        if deleted:
            reasons.append("track is soft-deleted")
        super().__init__(
            f"track {track_id}: refusing import: {'; '.join(reasons)}. Use "
            "activate_generation for generation changes or an explicit restore."
        )
        self.track_id = track_id
        self.generation = generation
        self.actual_generation = actual_generation
        self.deleted = deleted


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
        artist: str = "",
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
            artist=artist,
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
                    detail={"source_type": source_type.value},
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

    async def renew_lease(self, job_id: uuid.UUID, *, worker_id: str, lease_seconds: float) -> bool:
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

    async def park(
        self, job_id: uuid.UUID, *, worker_id: str, recheck_in_seconds: float
    ) -> None:
        """Schedule a claimed job for recheck without consuming an attempt."""
        now = utcnow()
        async with self._sf() as session, session.begin():
            await session.execute(
                update(Job)
                .where(Job.id == job_id, Job.lease_owner == worker_id)
                .values(
                    next_retry_at=now + timedelta(seconds=recheck_in_seconds),
                    lease_owner=None,
                    lease_expires_at=None,
                    updated_at=now,
                )
            )

    async def reserve_dispatch(
        self, job_id: uuid.UUID, *, worker_id: str, idempotency_key: str
    ) -> bool:
        """Durably reserve one RunPod submission while the caller owns the lease.

        The reservation commits before the external API call. A reclaimed lease
        therefore sees the outstanding reservation and must reconcile it instead
        of submitting another paid worker.
        """
        now = utcnow()
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.dispatched)
            lease_expires_at = _aware(job.lease_expires_at)
            if (
                job.status != JobStage.dispatched
                or job.lease_owner != worker_id
                or lease_expires_at is None
                or lease_expires_at <= now
            ):
                raise InvalidTransition(job_id, job.status, JobStage.dispatched)
            dispatch_events = (
                await session.execute(
                    select(JobEvent)
                    .where(
                        JobEvent.job_id == job_id,
                        JobEvent.event.in_(
                            (
                                _DISPATCH_STARTED_EVENT,
                                _DISPATCH_CONFIRMED_EVENT,
                                _LEGACY_DISPATCH_UNCONFIRMED_EVENT,
                            )
                        ),
                    )
                    .order_by(JobEvent.id)
                )
            ).scalars()
            if pending_runpod_dispatch(dispatch_events) is not None:
                return False
            session.add(
                JobEvent(
                    job_id=job_id,
                    event=_DISPATCH_STARTED_EVENT,
                    detail={
                        "idempotency_key": idempotency_key,
                        "attempt": job.attempt,
                    },
                )
            )
            return True

    async def confirm_dispatch(
        self, job_id: uuid.UUID, *, idempotency_key: str, runpod_job_id: str
    ) -> bool:
        """Attach a RunPod id to its durable reservation.

        Confirmation intentionally does not require the original lease. The
        external request may outlive that lease, but the latest reservation key
        prevents a stale dispatcher from overwriting a newer submission.
        """
        now = utcnow()
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None or job.status != JobStage.dispatched:
                current = job.status if job is not None else JobStage.failed
                raise InvalidTransition(job_id, current, JobStage.dispatched)
            dispatch_events = list(
                (
                    await session.execute(
                        select(JobEvent)
                        .where(
                            JobEvent.job_id == job_id,
                            JobEvent.event.in_(
                                (
                                    _DISPATCH_STARTED_EVENT,
                                    _DISPATCH_CONFIRMED_EVENT,
                                    _LEGACY_DISPATCH_UNCONFIRMED_EVENT,
                                )
                            ),
                        )
                        .order_by(JobEvent.id)
                    )
                ).scalars()
            )
            pending = pending_runpod_dispatch(dispatch_events)
            already_confirmed = any(
                event.event == _DISPATCH_CONFIRMED_EVENT
                and isinstance(event.detail, dict)
                and event.detail.get("idempotency_key") == idempotency_key
                and event.detail.get("runpod_job_id") == runpod_job_id
                for event in dispatch_events
            )
            if already_confirmed and job.runpod_job_id == runpod_job_id:
                return False
            detail = pending.detail if pending is not None else None
            if not isinstance(detail, dict) or detail.get("idempotency_key") != idempotency_key:
                raise InvalidTransition(job_id, job.status, JobStage.dispatched)
            job.runpod_job_id = runpod_job_id
            job.worker_phase = "dispatched"
            job.worker_heartbeat_at = now
            job.updated_at = now
            session.add(
                JobEvent(
                    job_id=job_id,
                    event=_DISPATCH_CONFIRMED_EVENT,
                    detail={
                        "runpod_job_id": runpod_job_id,
                        "idempotency_key": idempotency_key,
                    },
                )
            )
            return True

    async def record_dispatch(
        self, job_id: uuid.UUID, *, worker_id: str, runpod_job_id: str
    ) -> None:
        """Record a claimed dispatched job and seed its worker heartbeat."""
        now = utcnow()
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.dispatched)
            lease_expires_at = _aware(job.lease_expires_at)
            if (
                job.status != JobStage.dispatched
                or job.lease_owner != worker_id
                or lease_expires_at is None
                or lease_expires_at <= now
            ):
                raise InvalidTransition(job_id, job.status, JobStage.dispatched)
            job.runpod_job_id = runpod_job_id
            job.worker_phase = "dispatched"
            job.worker_heartbeat_at = now
            job.updated_at = now
            session.add(
                JobEvent(
                    job_id=job_id,
                    event="runpod_dispatched",
                    detail={"runpod_job_id": runpod_job_id},
                )
            )

    async def record_worker_progress(
        self, job_id: uuid.UUID, *, phase: str, worker_id: str
    ) -> bool:
        """Record only worker phase changes, keeping heartbeat history bounded.

        B2 sibling: a caller that no longer owns the lease is a stale poller;
        its write is a silent no-op rather than an error, so polling survives
        a reclaim without corrupting the new owner's heartbeat trail.
        """
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise LookupError(f"job {job_id} does not exist")
            if job.lease_owner != worker_id:
                return False
            if job.worker_phase == phase:
                # A stable work phase still proves liveness. Queue age is the
                # exception: its first heartbeat is the durable entry time used
                # by the queue-timeout watchdog and must not move on each poll.
                if phase != "queued":
                    now = utcnow()
                    job.worker_heartbeat_at = now
                    job.updated_at = now
                return False
            now = utcnow()
            job.worker_phase = phase
            job.worker_heartbeat_at = now
            job.updated_at = now
            session.add(JobEvent(job_id=job_id, event="worker_progress", detail={"phase": phase}))
            return True

    # --- state machine -------------------------------------------------------

    async def advance(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        from_stage: JobStage,
        to_stage: JobStage,
        duration_s: float | None = None,
        detail: dict[str, Any] | None = None,
        runpod_job_id: str | None = None,
        output_checksums: dict[str, Any] | None = None,
    ) -> Job:
        """Move a job one legal step forward; records timing and an event.

        Raises InvalidTransition when the row is not in `from_stage` anymore
        (e.g. a duplicate completion from a stale worker) or when the caller
        does not own an unexpired lease (B2) — callers treat both as a no-op
        signal, and no state is modified.
        """
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise InvalidTransition(job_id, JobStage.failed, to_stage)
            _require_lease(job, worker_id, to_stage, utcnow())
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
        """Record a retryable failure: attempt += 1, next_retry_at set, lease freed.

        B2: requires an unexpired lease owned by `worker_id`; a stale worker
        cannot burn an attempt or erase the new owner's lease.
        """
        now = utcnow()
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None or job.status in TERMINAL_STAGES:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.failed)
            _require_lease(job, worker_id, JobStage.failed, now)
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
        worker_id: str,
    ) -> Job:
        """Terminal failure from any non-terminal stage; idempotent on re-call.

        B2: `worker_id` is an ownership credential — the failure only lands if
        that worker still holds an unexpired lease. A duplicate report for an
        already-failed job remains a no-op regardless of the lease.
        """
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.failed)
            if job.status == JobStage.failed:
                return job  # duplicate failure report — no-op
            _require_lease(job, worker_id, JobStage.failed, utcnow())
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
        worker_id: str,
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
        event) converges on the same row and does not double-publish. A
        duplicate completion (job already ready, track row present) is a full
        no-write no-op: a ready job holds no lease, so its only possible
        caller is a stale writer, and B2 forbids stale event writes.

        Invariant C7: a location outside ``tracks/`` or without a
        ``/manifest.json`` key is refused — a ``publish_refused`` job event
        is recorded and :class:`PublishRefusedError` raised instead of
        publishing a phantom row.

        B2: ``worker_id`` must own an unexpired lease on every path that
        writes; a stale worker cannot record a refusal, clear the new
        owner's lease, or flip the job to ready.
        """
        problem = track_location_problem(s3_prefix, manifest_key)
        if problem is not None:
            async with self._sf() as session, session.begin():
                # Serialize concurrent refusals on the job row (FOR UPDATE,
                # the same lock the publish path takes): two racing refusals
                # run one after the other, so only the first passes the
                # existence check and records the event. Idempotent (B11): a
                # crash after this commit but before the stage fails the job
                # re-runs publish_track; the refusal is deterministic, so
                # record it once rather than appending a duplicate
                # publish_refused event per retry. B2: the event is a write,
                # so the lease fence runs under the same lock first — a
                # stale worker must not touch the new owner's history.
                job = await session.get(Job, job_id, with_for_update=True)
                if job is None:
                    raise InvalidTransition(job_id, JobStage.failed, JobStage.ready)
                _require_lease(job, worker_id, JobStage.ready, utcnow())
                already = await session.execute(
                    select(JobEvent).where(
                        JobEvent.job_id == job_id, JobEvent.event == "publish_refused"
                    )
                )
                if already.scalars().first() is None:
                    session.add(
                        JobEvent(
                            job_id=job_id,
                            event="publish_refused",
                            detail={
                                "s3_prefix": s3_prefix,
                                "manifest_key": manifest_key,
                                "reason": problem,
                            },
                        )
                    )
            raise PublishRefusedError(problem)
        tid = track_id_for_job(job_id)
        try:
            return await self._publish_track_once(
                job_id,
                tid,
                worker_id=worker_id,
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
        worker_id: str,
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
                # Duplicate completion — full no-op, and deliberately no
                # event: a ready job holds no lease (publish_track cleared
                # it), so any completion arriving now is from a stale writer
                # and B2 forbids stale writes. The idempotent return still
                # converges crash-reruns on the committed row.
                return existing

            _require_lease(job, worker_id, JobStage.ready, utcnow())
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
            # Invariant C7, defensive: a row whose objects are not under
            # tracks/ can never play (the 2026-08-19 phantom was a local/
            # placeholder), so the library never lists it.
            stmt = (
                select(Track)
                .where(Track.s3_prefix.startswith("tracks/"))
                .order_by(Track.created_at.desc(), Track.id)
            )
            if not include_deleted:
                stmt = stmt.where(Track.deleted_at.is_(None))
            res = await session.execute(stmt)
            return list(res.scalars().all())

    async def upsert_imported(
        self,
        track_id: uuid.UUID,
        *,
        title: str,
        artist: str = "",
        duration_seconds: float,
        s3_prefix: str,
        manifest_key: str,
        generation: int = 1,
        integrity: dict[str, Any] | None = None,
    ) -> tuple[Track, bool]:
        """Insert or refresh a track row that has no job behind it.

        Used by the library importer (`ops/import_legacy_library.py`),
        which publishes objects to S3 itself and then records the row. Jobs go
        through `JobRepository.publish_track` instead — that one also owns the
        publishing -> ready transition, which does not apply here.

        Returns `(track, created)`. Idempotent for a live row at the same
        generation: re-running the import with the same deterministic
        `track_id` updates the existing row in place, so the library converges
        rather than doubling. An existing row whose generation differs from
        `generation`, or that is soft-deleted, is refused with
        :class:`ImportConflict` — generation moves belong to
        `activate_generation` (invariant C5) and restores are explicit.
        """
        async with self._sf() as session, session.begin():
            track = await session.get(Track, track_id, with_for_update=True)
            created = track is None
            if track is None:
                track = Track(id=track_id, title=title, s3_prefix=s3_prefix, manifest_key="")
                session.add(track)
            elif track.generation != generation or track.deleted_at is not None:
                raise ImportConflict(
                    track_id, generation, track.generation, track.deleted_at is not None
                )
            track.title = title
            track.artist = artist
            track.duration_seconds = duration_seconds
            track.s3_prefix = s3_prefix
            track.generation = generation
            track.manifest_key = manifest_key
            track.integrity = integrity
            track.deleted_at = None
            return track, created

    async def activate_generation(
        self,
        track_id: uuid.UUID,
        *,
        expected_generation: int,
        generation: int,
        s3_prefix: str,
        manifest_key: str,
        integrity: dict[str, Any],
        event: str = "activated",
        detail: dict[str, Any] | None = None,
    ) -> Track:
        """Atomically switch a track only if its baseline is still current.

        The compare-and-swap prevents a slow batch migration from overwriting a
        concurrent repair. The activation event is committed in the same
        transaction, so there can be no pointer change without a rollback
        ledger entry.
        """
        if generation == expected_generation:
            raise ValueError("new generation must differ from expected generation")
        if event not in {"activated", "rollback"}:
            raise ValueError("event must be 'activated' or 'rollback'")
        async with self._sf() as session, session.begin():
            track = await session.get(Track, track_id, with_for_update=True)
            if track is None:
                raise LookupError(f"track {track_id} does not exist")
            if track.generation != expected_generation:
                raise TrackGenerationConflict(track_id, expected_generation, track.generation)
            prior = {
                "s3_prefix": track.s3_prefix,
                "manifest_key": track.manifest_key,
                "integrity": track.integrity,
            }
            track.generation = generation
            track.s3_prefix = s3_prefix.rstrip("/")
            track.manifest_key = manifest_key
            track.integrity = integrity
            session.add(
                TrackGenerationEvent(
                    track_id=track_id,
                    event=event,
                    from_generation=expected_generation,
                    to_generation=generation,
                    detail={"prior": prior, **(detail or {})},
                )
            )
            await session.flush()
            return track

    async def list_generation_events(self, track_id: uuid.UUID) -> list[TrackGenerationEvent]:
        async with self._sf() as session:
            result = await session.execute(
                select(TrackGenerationEvent)
                .where(TrackGenerationEvent.track_id == track_id)
                .order_by(TrackGenerationEvent.id)
            )
            return list(result.scalars().all())

    async def soft_delete(self, track_id: uuid.UUID) -> bool:
        """Mark deleted (design: no silent retention deletion; manual only)."""
        async with self._sf() as session, session.begin():
            track = await session.get(Track, track_id, with_for_update=True)
            if track is None or track.deleted_at is not None:
                return False
            track.deleted_at = utcnow()
            return True


class PlaybackTelemetryRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def ingest(
        self,
        *,
        session_id: uuid.UUID,
        track_id: uuid.UUID,
        generation: int,
        sequence: int,
        event: str,
        client_at_ms: int,
        app_build: str,
        browser: str,
        detail: dict[str, Any] | None,
    ) -> bool:
        """Insert an event once and update its session summary atomically."""
        async with self._sf() as session, session.begin():
            playback = await session.get(PlaybackSession, session_id, with_for_update=True)
            if playback is None:
                playback = PlaybackSession(
                    id=session_id,
                    track_id=track_id,
                    generation=generation,
                    app_build=app_build,
                    browser=browser,
                )
                session.add(playback)
                await session.flush()
            elif playback.track_id != track_id or playback.generation != generation:
                raise ValueError("playback session cannot change track or generation")

            existing = await session.scalar(
                select(PlaybackEvent.id).where(
                    PlaybackEvent.session_id == session_id,
                    PlaybackEvent.sequence == sequence,
                )
            )
            if existing is not None:
                return False
            if sequence <= playback.last_sequence:
                raise ValueError(
                    f"event sequence {sequence} is behind last sequence {playback.last_sequence}"
                )
            now = utcnow()
            playback.last_sequence = sequence
            playback.updated_at = now
            if event == "heartbeat":
                playback.last_heartbeat_at = now
            if event in {"ended", "fatal", "session-ended"}:
                playback.status = event
                playback.ended_at = now
            elif event in PLAYBACK_INCIDENT_EVENTS:
                playback.status = "incident"
            elif event in {"playing", "recovery-succeeded", "replay"}:
                playback.status = "playing"
            session.add(
                PlaybackEvent(
                    session_id=session_id,
                    track_id=track_id,
                    generation=generation,
                    sequence=sequence,
                    event=event,
                    client_at_ms=client_at_ms,
                    detail=detail,
                )
            )
            return True

    async def list_events(self, session_id: uuid.UUID) -> list[PlaybackEvent]:
        async with self._sf() as session:
            result = await session.execute(
                select(PlaybackEvent)
                .where(PlaybackEvent.session_id == session_id)
                .order_by(PlaybackEvent.sequence)
            )
            return list(result.scalars().all())

    async def get_session(self, session_id: uuid.UUID) -> PlaybackSession | None:
        async with self._sf() as session:
            return await session.get(PlaybackSession, session_id)

    async def recent_incidents(
        self, *, since: datetime, limit: int = 100
    ) -> list[tuple[PlaybackEvent, str]]:
        """Return directly observed incidents with their human track title."""
        async with self._sf() as session:
            result = await session.execute(
                select(PlaybackEvent, Track.title)
                .join(Track, Track.id == PlaybackEvent.track_id)
                .where(
                    PlaybackEvent.received_at >= since,
                    PlaybackEvent.event.in_(PLAYBACK_INCIDENT_EVENTS),
                )
                .order_by(PlaybackEvent.received_at.desc(), PlaybackEvent.id.desc())
                .limit(limit)
            )
            return [(event, title) for event, title in result.all()]


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

"""Typed repository layer — the only place SQL happens.

Routes and the orchestrator call these methods; sessions never leak out.
Every public method is its own transaction against the injected session
factory, so callers cannot half-commit orchestration state.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql import Select

from ..errors import ErrorCode
from .models import (
    ADMIN_RETRY_TRANSITION,
    ALLOWED_TRANSITIONS,
    NODE_ASSIGNMENT_OPEN,
    RUNNABLE_STAGES,
    TERMINAL_STAGES,
    Job,
    JobEvent,
    JobStage,
    Node,
    NodeAssignment,
    NodeAssignmentKind,
    NodeAssignmentStatus,
    OrchestratorHeartbeat,
    PlaybackEvent,
    PlaybackSession,
    RuntimeSetting,
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
_ADMIN_RETRY_STARTED_EVENT = "admin_retry_started"

# Advisory-lock key for the RunPod capacity gate (Postgres only): every
# capacity-gated reservation takes this xact lock, so two orchestrator
# instances can never both observe the same count and both dispatch into the
# last slot. The lock dies with the transaction.
_RUNPOD_CAPACITY_ADVISORY_KEY = 727501


def _runpod_work_is_live(job: Job) -> bool:
    """Whether a confirmed ``runpod_job_id`` still names live provider work.

    A worker the orchestrator marked failed (B12: treated as absent, the
    retry dispatches fresh) is a closed execution — counting it would make a
    retry count itself forever. Node executor ids are never RunPod work.
    """
    if not job.runpod_job_id or job.runpod_job_id.startswith("node:"):
        return False
    return job.worker_phase != "failed"


async def _count_runpod_work(session: AsyncSession) -> int:
    """Jobs currently holding a RunPod reservation or confirmed RunPod job.

    Semantics (G3) — unresolved reservations are inspected FIRST, because a
    row can retain the artifacts of a closed execution (an old
    ``runpod_job_id``, ``worker_phase='failed'``) while a retry's fresh
    reservation is already outstanding:
    - an unresolved reservation counts (it may be accepted at RunPod: a
      submit timeout leaves it outstanding, B3) unless it is provably
      node-bound — proof is a separation assignment carrying the
      reservation's idempotency key (assign_separation creates it just
      before reserving). A retained ``executor = node:<id>`` from an earlier
      URL download is NOT proof: the download assignment lives under a
      different key;
    - otherwise the confirmed execution counts while its worker is not
      marked failed — a failed worker is a closed execution whose retry
      dispatches fresh (B12) and must not count itself.
    """
    # One statement is deliberate: on PostgreSQL READ COMMITTED, separate
    # job/event queries can observe different commits. A confirmation landing
    # between them used to hide live work from the capacity gate (G3). The
    # joins also replace the old per-job event/assignment query scan.
    res = await session.execute(
        select(Job, JobEvent, NodeAssignment)
        .outerjoin(
            JobEvent,
            and_(
                JobEvent.job_id == Job.id,
                JobEvent.event.in_(
                    (
                        _DISPATCH_STARTED_EVENT,
                        _DISPATCH_CONFIRMED_EVENT,
                        _LEGACY_DISPATCH_UNCONFIRMED_EVENT,
                        _ADMIN_RETRY_STARTED_EVENT,
                    )
                ),
            ),
        )
        .outerjoin(
            NodeAssignment,
            and_(
                NodeAssignment.job_id == Job.id,
                NodeAssignment.kind == NodeAssignmentKind.separate,
            ),
        )
        .where(Job.status == JobStage.dispatched)
        .order_by(Job.id, JobEvent.id, NodeAssignment.id)
    )
    jobs: dict[uuid.UUID, Job] = {}
    events: dict[uuid.UUID, dict[int, JobEvent]] = {}
    assignment_keys: dict[uuid.UUID, set[str]] = {}
    for job, event, assignment in res.all():
        jobs[job.id] = job
        if event is not None:
            events.setdefault(job.id, {})[event.id] = event
        if assignment is not None and assignment.idempotency_key is not None:
            assignment_keys.setdefault(job.id, set()).add(assignment.idempotency_key)

    count = 0
    for job in jobs.values():
        job_events = sorted(events.get(job.id, {}).values(), key=lambda event: event.id)
        pending = pending_runpod_dispatch(job_events)
        if pending is not None:
            pending_detail = pending.detail if isinstance(pending.detail, dict) else {}
            pending_key = pending_detail.get("idempotency_key")
            if isinstance(pending_key, str) and pending_key in assignment_keys.get(job.id, set()):
                # Node-bound reservation: not RunPod capacity, and the
                # closed previous execution below must not count either.
                continue
            count += 1
            continue
        if _runpod_work_is_live(job):
            count += 1
    return count

logger = logging.getLogger(__name__)


def pending_runpod_dispatch(events: Iterable[JobEvent]) -> JobEvent | None:
    """Return the latest dispatch that may have been accepted but is unconfirmed.

    ``dispatch_unconfirmed`` predates durable reservations. It must remain
    fail-closed during a rolling upgrade; otherwise an in-flight legacy job
    could be submitted again by the first process running the new code.
    """
    pending: JobEvent | None = None
    pending_key: str | None = None
    retry_attempt: int | None = None
    for event in events:
        detail = event.detail if isinstance(event.detail, dict) else {}
        event_key = detail.get("idempotency_key")
        if event.event == "admin_retry_started":
            # Only a fresh pending restart opens a new dispatch authority
            # window. Publication recovery resumes the SAME reservation and
            # must not erase its handoff evidence.
            if detail.get("resume_stage") != JobStage.pending.value:
                continue
            pending = None
            pending_key = None
            new_attempt = detail.get("new_attempt")
            retry_attempt = new_attempt if isinstance(new_attempt, int) else None
            continue
        attempt = detail.get("attempt")
        if (
            retry_attempt is not None and isinstance(attempt, int)
            and attempt < retry_attempt
        ):
            continue
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


def _retry_reservation_is_resolved(
    pending: JobEvent,
    runpod_outcome: dict[str, Any],
    historical: list[JobEvent],
) -> bool:
    """Bind API evidence to the exact reservation + failure under lock."""
    latest_failure = next(
        (event for event in reversed(historical) if event.event == "failed"),
        None,
    )
    return (
        runpod_outcome.get("status") == "resolved_reservation"
        and runpod_outcome.get("reservation_event_id") == pending.id
        and latest_failure is not None
        and runpod_outcome.get("failure_event_id") == latest_failure.id
        and runpod_outcome.get("reservation_resolution")
        in {"provider_rejected", "no_receipt_after_deadline"}
    )


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


def _has_live_lease(job: Job | None, now: datetime) -> bool:
    if job is None:
        return False
    expires = _aware(job.lease_expires_at)
    return expires is not None and expires > now


def _lease_is_owned(job: Job, worker_id: str, now: datetime) -> bool:
    """B2 ownership test: the caller must be the recorded owner of an
    unexpired lease (None or past expiry fails, mirroring record_dispatch)."""
    lease_expires_at = _aware(job.lease_expires_at)
    return (
        job.lease_owner == worker_id
        and lease_expires_at is not None
        and lease_expires_at > now
    )


def _require_lease(job: Job, worker_id: str, target: JobStage, now: datetime) -> None:
    """B2 fence for stage-outcome writes: inside the locked transaction, the
    caller must still own an unexpired lease, exactly like record_dispatch."""
    if not _lease_is_owned(job, worker_id, now):
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


def recoverable_publication_failure(job: Job, events: list[JobEvent]) -> bool:
    """Retain explicit recovery eligibility after a verification outage."""
    failures = [event for event in events if event.event == "failed"]
    if job.status != JobStage.failed or not failures:
        return False
    failure = failures[-1]
    stage = (failure.detail or {}).get("stage")
    if stage == "publishing" and job.error_code == ErrorCode.PUBLISH_FAILED.value:
        return True
    if stage == "verifying":
        # Admin retry may resume a selected package after verification failed;
        # provider completion and immutable receipts are checked by the caller.
        # A publication code on a newer verifying failure is inconsistent
        # provenance, not a reason to bypass the existing publication guard.
        return job.error_code != ErrorCode.PUBLISH_FAILED.value
    recoveries = [event for event in events if event.event == "publication_recovery_started"
                  and event.id < failure.id]
    if not recoveries or stage not in {"verifying", "publishing"}:
        return False
    provenance = recoveries[-1].detail or {}
    return (provenance.get("provider_job_id") == job.runpod_job_id
            and provenance.get("input_checksum") == job.input_checksum)


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

    async def get_retry_snapshot(
        self, job_id: uuid.UUID
    ) -> tuple[Job | None, list[JobEvent]]:
        """Read retry state and its event authority at one locked instant.

        Dismissal/resubmission preflight uses the latest dismissal event as a
        fence. Reading it separately from the job could pair an old row with a
        newer event and authorize stale source evidence. Every competing
        terminal action takes this row lock, so the short snapshot is coherent.
        """
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update={"skip_locked": True})
            if job is None:
                # A live competitor must produce retry's normal 409, not wait
                # behind a test/request that is itself awaiting this response.
                # Jobs are never hard-deleted, so the unlocked read safely
                # distinguishes that case from an unknown id.
                existing = await session.get(Job, job_id)
                if existing is not None:
                    raise InvalidTransition(job_id, JobStage.failed, JobStage.pending)
                return None, []
            events = list((await session.scalars(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
            )).all())
            return job, events

    async def get_job_by_idempotency_key(self, key: str) -> Job | None:
        async with self._sf() as session:
            res = await session.execute(select(Job).where(Job.idempotency_key == key))
            return res.scalar_one_or_none()

    async def list_jobs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: JobStage | None = None,
        dismissed: str | None = None,
    ) -> tuple[list[Job], int]:
        """Newest first with status and dismissed visibility filters."""
        if dismissed not in (None, "include", "only"):
            raise ValueError("dismissed must be include or only")

        def filtered(statement: Select[Any]) -> Select[Any]:
            if status is not None:
                statement = statement.where(Job.status == status)
            if dismissed == "only":
                statement = statement.where(Job.dismissed_at.is_not(None))
            elif dismissed != "include":
                statement = statement.where(Job.dismissed_at.is_(None))
            return statement

        async with self._sf() as session:
            total = (await session.execute(filtered(select(func.count(Job.id))))).scalar_one()
            query = filtered(select(Job)).order_by(Job.created_at.desc(), Job.id)
            res = await session.execute(query.limit(limit).offset(offset))
            return list(res.scalars().all()), total

    async def list_all_jobs(self) -> list[Job]:
        """All jobs, including dismissed rows, for administrator duplicate review."""
        async with self._sf() as session:
            result = await session.execute(select(Job).order_by(Job.created_at, Job.id))
            return list(result.scalars().all())

    async def count_jobs_by_status(self) -> dict[JobStage, int]:
        async with self._sf() as session:
            res = await session.execute(
                select(Job.status, func.count(Job.id))
                .where(Job.dismissed_at.is_(None))
                .group_by(Job.status)
            )
            return {row[0]: int(row[1]) for row in res.all()}

    async def count_dismissed_jobs(self) -> int:
        async with self._sf() as session:
            return int((await session.execute(
                select(func.count(Job.id)).where(Job.dismissed_at.is_not(None))
            )).scalar_one())

    async def count_runpod_dispatches(self) -> int:
        """Jobs currently holding a RunPod reservation or confirmed RunPod job
        (see ``_count_runpod_work`` for the exact semantics). Work already
        dispatched is never cancelled to make room — this count only gates
        FRESH dispatches."""
        async with self._sf() as session:
            return await _count_runpod_work(session)

    async def list_events(self, job_id: uuid.UUID) -> list[JobEvent]:
        async with self._sf() as session:
            res = await session.execute(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
            )
            return list(res.scalars().all())

    async def list_events_for_jobs(
        self, job_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, list[JobEvent]]:
        """Batch job histories for admin lists (one query, not one per row)."""
        ids = list(dict.fromkeys(job_ids))
        if not ids:
            return {}
        async with self._sf() as session:
            res = await session.execute(
                select(JobEvent)
                .where(JobEvent.job_id.in_(ids))
                .order_by(JobEvent.job_id, JobEvent.id)
            )
            grouped: dict[uuid.UUID, list[JobEvent]] = {job_id: [] for job_id in ids}
            for event in res.scalars():
                grouped[event.job_id].append(event)
            return grouped

    async def append_event(
        self,
        job_id: uuid.UUID,
        event: str,
        detail: dict[str, Any] | None = None,
        *,
        worker_id: str | None = None,
    ) -> None:
        """Append an operational event to a job's history.

        B2: when ``worker_id`` is given (worker-context callers), the append
        is fenced — the job row is loaded under lock and nothing is recorded
        unless that worker owns an unexpired lease, so an evicted worker
        cannot leave misleading operational history. API and other
        non-worker callers pass no ``worker_id`` and append unconditionally.
        """
        async with self._sf() as session, session.begin():
            if worker_id is not None:
                job = await session.get(Job, job_id, with_for_update=True)
                if job is None or not _lease_is_owned(job, worker_id, utcnow()):
                    logger.debug(
                        "job %s: skipped %s event from worker %s without a live lease",
                        job_id,
                        event,
                        worker_id,
                    )
                    return
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
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        idempotency_key: str,
        max_concurrent: int | None = None,
    ) -> bool:
        """Durably reserve one RunPod submission while the caller owns the lease.

        The reservation commits before the external API call. A reclaimed lease
        therefore sees the outstanding reservation and must reconcile it instead
        of submitting another paid worker.

        With ``max_concurrent`` set (the admin RunPod cap, G3) the capacity
        check is ATOMIC with the reservation: on Postgres every gated
        reservation first takes the capacity advisory xact lock, so two
        orchestrator instances racing for the last slot serialize — one
        reserves, the other sees the committed count and returns False
        (the caller parks). Node reservations pass no cap: they never consume
        RunPod capacity.
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
            if max_concurrent is not None:
                bind = session.bind
                if bind is not None and bind.dialect.name == "postgresql":
                    await session.execute(
                        text(
                            "SELECT pg_advisory_xact_lock(:key)"
                        ),
                        {"key": _RUNPOD_CAPACITY_ADVISORY_KEY},
                    )
                if await _count_runpod_work(session) >= max_concurrent:
                    return False
            # Row/advisory lock waits can outlive the lease captured at entry.
            # The row lock prevents reclaim during this transaction, but it
            # must not authorize a paid submission after the lease expires.
            if lease_expires_at <= utcnow():
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
                                _ADMIN_RETRY_STARTED_EVENT,
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
                                    _ADMIN_RETRY_STARTED_EVENT,
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
            # A node assignment set executor to node:<id> when it was created;
            # a RunPod confirmation (first choice or fallback) records RunPod so
            # the API never reports a stale node as the executor.
            if not runpod_job_id.startswith("node:"):
                job.executor = "runpod"
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

        B2 sibling: only the owner of an unexpired lease may write — an
        expired-but-unreclaimed lease must not pass dead work off as live.
        A caller without a live lease is a stale poller; its write is a
        silent no-op rather than an error, so polling survives a reclaim
        without corrupting the new owner's heartbeat trail.
        """
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise LookupError(f"job {job_id} does not exist")
            if not _lease_is_owned(job, worker_id, utcnow()):
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

    async def record_publish_step(
        self,
        job_id: uuid.UUID,
        *,
        phase: str,
        worker_id: str,
    ) -> bool:
        """Record one changed publication phase and its cheap timing event.

        The phase and append-only ``publish_step`` event commit together under
        the same lease fence as other worker progress. Repeating a step after
        a crash is a no-op while it remains the durable current phase (B8/B11).
        """
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise LookupError(f"job {job_id} does not exist")
            now = utcnow()
            if not _lease_is_owned(job, worker_id, now) or job.worker_phase == phase:
                return False
            job.worker_phase = phase
            job.worker_heartbeat_at = now
            job.updated_at = now
            previous = await session.scalar(
                select(JobEvent)
                .where(JobEvent.job_id == job_id, JobEvent.event == "publish_step")
                .order_by(JobEvent.id.desc())
                .limit(1)
            )
            previous_detail = (
                previous.detail if previous is not None and isinstance(previous.detail, dict) else {}
            )
            previous_step = previous_detail.get("step")
            previous_seconds = (
                round(max(0.0, (now - (_aware(previous.created_at) or now)).total_seconds()), 3)
                if previous is not None
                else 0.0
            )
            session.add(JobEvent(job_id=job_id, event="worker_progress", detail={"phase": phase}))
            session.add(JobEvent(
                job_id=job_id,
                event="publish_step",
                detail={
                    "step": phase,
                    "previous_step": previous_step,
                    "previous_seconds": previous_seconds,
                },
            ))
            return True

    async def set_input_checksum(
        self, job_id: uuid.UUID, *, worker_id: str, checksum: str
    ) -> None:
        """Record the source sha256 a node proved (A12); lease-fenced (B2)."""
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.downloading)
            _require_lease(job, worker_id, job.status, utcnow())
            job.input_checksum = checksum
            job.updated_at = utcnow()

    async def set_metadata(
        self, job_id: uuid.UUID, *, worker_id: str, title: str | None, artist: str | None
    ) -> None:
        """Fill title/artist a node learned from the source; never overwrite user text."""
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.downloading)
            _require_lease(job, worker_id, job.status, utcnow())
            if title and not job.title:
                job.title = title
            if artist and not job.artist:
                job.artist = artist
            job.updated_at = utcnow()

    async def owns_lease(self, job_id: uuid.UUID, worker_id: str) -> bool:
        """B2 predicate: does this worker own the job's unexpired lease now?

        For callers that gate remote effects (e.g. cancelling the RunPod job)
        rather than writes: unlike ``record_worker_progress`` it cannot
        conflate "not owner" with "phase unchanged".
        """
        async with self._sf() as session:
            job = await session.get(Job, job_id)
            return job is not None and _lease_is_owned(job, worker_id, utcnow())

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
            if from_stage == JobStage.dispatched and to_stage == JobStage.verifying:
                # Separation's last upload percentage is not verification
                # progress. The verifier records its first real step next.
                job.worker_phase = None
                job.worker_heartbeat_at = None
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
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None or job.status in TERMINAL_STAGES:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.failed)
            # B2: the expiry comparison uses a timestamp taken after the row
            # lock is held, so a lock wait cannot make an expired lease pass.
            now = utcnow()
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
        provider_accepted: bool | None = None,
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
            executor_closed = job.worker_phase == "failed"
            job.status = JobStage.failed
            job.error_code = error_code.value
            job.error_detail = error_detail
            job.lease_owner = None
            job.lease_expires_at = None
            job.next_retry_at = None
            job.worker_phase = None
            job.worker_heartbeat_at = None
            job.updated_at = utcnow()
            failure_detail: dict[str, Any] = {
                "stage": from_stage.value,
                "error_code": error_code.value,
                "error_detail": error_detail[:500],
                "worker": worker_id,
            }
            if provider_accepted is not None:
                failure_detail["provider_accepted"] = provider_accepted
                if from_stage == JobStage.dispatched:
                    failure_detail["idempotency_key"] = f"{job.id.hex}:{job.attempt}"
            if from_stage == JobStage.dispatched and job.runpod_job_id is not None:
                # Terminal cards no longer display the executor phase. Preserve
                # its closed/not-closed meaning as durable attempt evidence for
                # admin retry instead of relying on the cleared UI field.
                failure_detail["runpod_job_id"] = job.runpod_job_id
                failure_detail["executor_closed"] = executor_closed
            session.add(
                JobEvent(
                    job_id=job.id,
                    event="failed",
                    detail=failure_detail,
                )
            )
            return job

    async def _expire_open_assignments(
        self,
        session: AsyncSession,
        job_id: uuid.UUID,
        *,
        reason: str,
        now: datetime,
    ) -> list[str]:
        assignments = list((await session.scalars(
            select(NodeAssignment)
            .where(
                NodeAssignment.job_id == job_id,
                NodeAssignment.status.in_(tuple(NODE_ASSIGNMENT_OPEN)),
            )
            .with_for_update()
        )).all())
        expired: list[str] = []
        nodes = NodeRepository(self._sf)
        for assignment in assignments:
            assignment.status = NodeAssignmentStatus.expired
            assignment.detail = {**(assignment.detail or {}), "expired_reason": reason}
            assignment.updated_at = now
            await nodes._free_node(session, assignment, now)
            expired.append(str(assignment.id))
        return expired

    async def expire_terminal_assignments(
        self, job_id: uuid.UUID, *, reason: str
    ) -> list[str] | None:
        """Expire open work before publication recovery, fenced by the job lock."""
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            now = utcnow()
            if (
                job is None or job.status != JobStage.failed
                or _has_live_lease(job, now)
            ):
                return None
            return await self._expire_open_assignments(session, job_id, reason=reason, now=now)

    async def admin_restart_job(
        self,
        job_id: uuid.UUID,
        *,
        reason: str,
        runpod_outcome: dict[str, Any],
        fallback_reason: str | None = None,
        restore: bool = False,
        expected_dismissal_event_id: int | None = None,
    ) -> Job:
        """Atomically reopen a failed job at pending under ADMIN_RETRY_TRANSITION.

        The attempt increases monotonically, so the next dispatch key is fresh
        (B12). Open node work is expired in the same transaction; history is
        append-only and ordinary advance() remains terminal-job fenced.
        """
        async with self._sf() as session, session.begin():
            result = await session.execute(
                select(Job).where(Job.id == job_id).with_for_update(skip_locked=True)
            )
            job = result.scalar_one_or_none()
            now = utcnow()
            if (
                job is None or job.status != JobStage.failed
                or (job.dismissed_at is not None and not restore)
                or _has_live_lease(job, now)
            ):
                raise InvalidTransition(job_id, JobStage.failed, JobStage.pending)
            previous_code = job.error_code
            historical = list((await session.scalars(
                select(JobEvent).where(JobEvent.job_id == job.id).order_by(JobEvent.id)
            )).all())
            if restore:
                latest_dismissal = next(
                    (event for event in reversed(historical) if event.event == "admin_dismissed"),
                    None,
                )
                if (latest_dismissal.id if latest_dismissal is not None else None) != expected_dismissal_event_id:
                    # Source/package preflight ran before this lock. A newer
                    # dismissal may have cleaned those bytes; never restore from
                    # stale evidence. A standalone restore adds no dismissal and
                    # is therefore safe to join.
                    raise InvalidTransition(job_id, JobStage.failed, JobStage.pending)
            pending = pending_runpod_dispatch(historical)
            if (
                pending is None
                and runpod_outcome.get("reservation_event_id") is not None
            ) or (
                pending is not None and not _retry_reservation_is_resolved(
                    pending, runpod_outcome, historical
                )
            ):
                # Missing provider id is not proof that an accepted request
                # never existed. Only the exact preflighted reservation may
                # be reopened, and the event id closes prefill/commit races.
                raise InvalidTransition(job_id, JobStage.failed, JobStage.pending)
            expired = await self._expire_open_assignments(
                session, job_id, reason="expired by administrator retry", now=now
            )
            job.status = ADMIN_RETRY_TRANSITION[1]
            job.attempt += 1
            job.error_code = None
            job.error_detail = None
            job.next_retry_at = None
            job.lease_owner = None
            job.lease_expires_at = None
            job.runpod_job_id = None
            job.executor = None
            job.worker_phase = None
            job.worker_heartbeat_at = None
            job.output_checksums = None
            job.stage_timings = None
            job.updated_at = now
            if restore and job.dismissed_at is not None:
                job.dismissed_at = None
                session.add(JobEvent(
                    job_id=job.id,
                    event="admin_restored",
                    detail={"expired_assignment_ids": [], "retry": True},
                ))
            session.add(JobEvent(
                job_id=job.id,
                event="admin_retry_started",
                detail={
                    "previous_failure_code": previous_code,
                    "resume_stage": JobStage.pending.value,
                    "reason": reason,
                    "fallback_reason": fallback_reason,
                    "new_attempt": job.attempt,
                    "reservation_resolution": runpod_outcome.get("reservation_resolution"),
                    "dispatch_key": f"{job.id.hex}:{job.attempt}",
                    "expired_assignment_ids": expired,
                    "runpod": runpod_outcome,
                },
            ))
            return job

    async def admin_resubmit_url_job(
        self,
        job_id: uuid.UUID,
        *,
        runpod_outcome: dict[str, Any],
        fallback_reason: str | None = None,
        restore: bool = False,
        expected_dismissal_event_id: int | None = None,
    ) -> Job:
        """Create a fresh URL job and dismiss its failed predecessor atomically."""
        async with self._sf() as session, session.begin():
            result = await session.execute(
                select(Job).where(Job.id == job_id).with_for_update(skip_locked=True)
            )
            old = result.scalar_one_or_none()
            now = utcnow()
            if (
                old is None or old.status != JobStage.failed
                or old.source_type != SourceType.url
                or (old.dismissed_at is not None and not restore)
                or _has_live_lease(old, now)
            ):
                raise InvalidTransition(job_id, JobStage.failed, JobStage.pending)
            historical = list((await session.scalars(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
            )).all())
            if any(event.event == "admin_resubmitted" for event in historical):
                # The predecessor stays failed+dismissed by design. Its
                # immutable completion evidence, not that persistent state,
                # decides whether another fresh URL job may be created.
                raise InvalidTransition(job_id, JobStage.failed, JobStage.pending)
            if restore:
                latest_dismissal = next(
                    (event for event in reversed(historical) if event.event == "admin_dismissed"),
                    None,
                )
                if (latest_dismissal.id if latest_dismissal is not None else None) != expected_dismissal_event_id:
                    raise InvalidTransition(job_id, JobStage.failed, JobStage.pending)
            pending = pending_runpod_dispatch(historical)
            if (
                pending is None
                and runpod_outcome.get("reservation_event_id") is not None
            ) or (
                pending is not None and not _retry_reservation_is_resolved(
                    pending, runpod_outcome, historical
                )
            ):
                raise InvalidTransition(job_id, JobStage.failed, JobStage.pending)
            expired = await self._expire_open_assignments(
                session, job_id, reason="expired by administrator URL resubmission", now=now
            )
            new = Job(
                id=uuid.uuid4(),
                source_type=SourceType.url,
                source_ref=old.source_ref,
                title=old.title,
                artist=old.artist,
                status=JobStage.pending,
                idempotency_key=uuid.uuid4().hex,
                profile_version=old.profile_version,
            )
            session.add(new)
            await session.flush()
            if restore and old.dismissed_at is not None:
                session.add(JobEvent(
                    job_id=old.id,
                    event="admin_restored",
                    detail={"expired_assignment_ids": [], "retry": True},
                ))
            old.dismissed_at = now
            old.updated_at = now
            session.add(JobEvent(
                job_id=old.id,
                event="admin_resubmitted",
                detail={
                    "new_job_id": str(new.id),
                    "previous_failure_code": old.error_code,
                    "fallback_reason": fallback_reason,
                    "reservation_resolution": runpod_outcome.get("reservation_resolution"),
                    "expired_assignment_ids": expired,
                    "runpod": runpod_outcome,
                },
            ))
            session.add(JobEvent(
                job_id=old.id,
                event="admin_dismissed",
                detail={"expired_assignment_ids": expired, "resubmitted_as": str(new.id)},
            ))
            session.add(JobEvent(
                job_id=new.id,
                event="created",
                detail={"source_type": SourceType.url.value},
            ))
            session.add(JobEvent(
                job_id=new.id,
                event="admin_retry_started",
                detail={
                    "previous_failure_code": old.error_code,
                    "resume_stage": JobStage.pending.value,
                    "reason": "resubmit URL as a fresh job",
                    "new_attempt": new.attempt,
                    "reservation_resolution": runpod_outcome.get("reservation_resolution"),
                    "dispatch_key": f"{new.id.hex}:{new.attempt}",
                },
            ))
            session.add(JobEvent(
                job_id=new.id,
                event="admin_resubmitted_from",
                detail={"old_job_id": str(old.id)},
            ))
            return new

    async def set_job_dismissed(
        self,
        job_id: uuid.UUID,
        *,
        dismissed: bool,
        cleanup: Callable[[], Awaitable[None]] | None = None,
    ) -> Job:
        """Finish cleanup and dismissal even if the caller is cancelled."""
        operation = asyncio.create_task(
            self._set_job_dismissed_locked(job_id, dismissed=dismissed, cleanup=cleanup)
        )
        cancelled = False
        while not operation.done():
            try:
                await asyncio.shield(operation)
            except asyncio.CancelledError:
                # Every cancellation is kept away from the locked operation,
                # not only the first. Keep waiting until cleanup + commit end.
                cancelled = True
        result = await operation
        current = asyncio.current_task()
        if cancelled or (current is not None and current.cancelling()):
            raise asyncio.CancelledError
        return result

    async def _set_job_dismissed_locked(
        self,
        job_id: uuid.UUID,
        *,
        dismissed: bool,
        cleanup: Callable[[], Awaitable[None]] | None = None,
    ) -> Job:
        """Dismiss/restore under one lock; cleanup precedes the commit."""
        async with self._sf() as session, session.begin():
            result = await session.execute(
                select(Job).where(Job.id == job_id).with_for_update(skip_locked=True)
            )
            job = result.scalar_one_or_none()
            now = utcnow()
            if job is None or job.status != JobStage.failed:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.failed)
            if dismissed and job.dismissed_at is not None:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.failed)
            if not dismissed and job.dismissed_at is None:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.failed)
            if dismissed and _has_live_lease(job, now):
                raise InvalidTransition(job_id, JobStage.failed, JobStage.failed)
            if dismissed and cleanup is not None:
                await cleanup()
            expired: list[str] = []
            if dismissed:
                expired = await self._expire_open_assignments(
                    session, job_id, reason="expired by administrator dismissal", now=now
                )
            job.dismissed_at = now if dismissed else None
            job.updated_at = now
            session.add(JobEvent(
                job_id=job.id,
                event="admin_dismissed" if dismissed else "admin_restored",
                detail={"expired_assignment_ids": expired},
            ))
            return job

    async def recover_publication(
        self,
        job_id: uuid.UUID,
        *,
        expected_provider_id: str | None,
        expected_checksum: str,
        expected_idempotency_key: str,
        expected_dispatch_key: str,
        recovery_id: uuid.UUID,
        worker_id: str,
        lease_seconds: float,
        admin_retry: bool = False,
        restore: bool = False,
        expected_dismissal_event_id: int | None = None,
        expected_reservation_id: int | None = None,
        expected_failure_event_id: int | None = None,
        reservation_resolution: str | None = None,
    ) -> Job:
        """Operator-only claim of a proven failed publication; never dispatch.

        The recovery command first verifies provider completion and package
        bytes. This transaction rechecks its exact input/dispatch identity and
        failure history before granting a lease for verification/publication.
        Normal advance() still treats failed jobs as terminal.
        """
        if not worker_id or not 0 < lease_seconds <= 3600:
            raise ValueError("A bounded recovery lease and owner are required")
        handoff_recovery = reservation_resolution == "handoff"
        if reservation_resolution is not None and not handoff_recovery:
            raise ValueError("Only handoff evidence can recover an unconfirmed reservation")
        async with self._sf() as session, session.begin():
            result = await session.execute(
                select(Job).where(Job.id == job_id).with_for_update(skip_locked=True)
            )
            job = result.scalar_one_or_none()
            now = utcnow()
            if (
                job is None or job.status != JobStage.failed
                or (not handoff_recovery and not expected_provider_id)
                or not expected_checksum or not expected_idempotency_key
                or (
                    not handoff_recovery
                    and job.runpod_job_id != expected_provider_id
                )
                or (
                    handoff_recovery
                    and (
                        expected_reservation_id is None
                        or expected_failure_event_id is None
                    )
                )
                or job.input_checksum != expected_checksum
                or job.idempotency_key != expected_idempotency_key
                or job.track_id is not None
                or (job.dismissed_at is not None and not restore)
                or (job.lease_expires_at is not None and _aware(job.lease_expires_at) > now)
            ):
                raise InvalidTransition(job_id, JobStage.failed, JobStage.verifying)
            events = list((await session.scalars(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.id)
            )).all())
            if restore:
                latest_dismissal = next(
                    (event for event in reversed(events) if event.event == "admin_dismissed"),
                    None,
                )
                if (latest_dismissal.id if latest_dismissal is not None else None) != expected_dismissal_event_id:
                    raise InvalidTransition(job_id, JobStage.failed, JobStage.verifying)
            failure = next(
                (event for event in reversed(events) if event.event == "failed"),
                None,
            )
            if failure is None or any(
                event.event == "publication_recovery_started"
                and (event.detail or {}).get("recovery_id") == str(recovery_id)
                for event in events
            ):
                raise InvalidTransition(job_id, JobStage.failed, JobStage.verifying)
            if handoff_recovery:
                pending = pending_runpod_dispatch(events)
                pending_detail = pending.detail if pending is not None else None
                failure_detail = (
                    failure.detail if isinstance(failure.detail, dict) else {}
                )
                prior_handoff_recovery = any(
                    event.id < failure.id
                    and event.event == "admin_retry_started"
                    and isinstance(event.detail, dict)
                    and event.detail.get("reservation_resolution") == "handoff"
                    and event.detail.get("dispatch_key") == expected_dispatch_key
                    for event in events
                )
                if (
                    pending is None or not isinstance(pending_detail, dict)
                    or pending.id != expected_reservation_id
                    or failure.id != expected_failure_event_id
                    or pending_detail.get("idempotency_key") != expected_dispatch_key
                    or (
                        failure_detail.get("stage") != JobStage.dispatched.value
                        and not prior_handoff_recovery
                    )
                ):
                    raise InvalidTransition(job_id, JobStage.failed, JobStage.verifying)
            else:
                if not recoverable_publication_failure(job, events):
                    raise InvalidTransition(job_id, JobStage.failed, JobStage.verifying)
                dispatch = await session.scalar(
                    select(JobEvent)
                    .where(
                        JobEvent.job_id == job_id,
                        JobEvent.event == "runpod_dispatched",
                    )
                    .order_by(JobEvent.id.desc()).limit(1)
                )
                if (
                    dispatch is None or not expected_dispatch_key
                    or (dispatch.detail or {}).get("idempotency_key") != expected_dispatch_key
                    or (dispatch.detail or {}).get("runpod_job_id") != expected_provider_id
                ):
                    raise InvalidTransition(job_id, JobStage.failed, JobStage.verifying)
            if await session.get(Track, track_id_for_job(job.id)) is not None:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.verifying)
            # Preserve the reservation/provider ID and attempt counter. A
            # crash after commit can only resume verification/publication.
            previous_code = job.error_code
            job.status = JobStage.verifying
            job.lease_owner = worker_id
            job.lease_expires_at = now + timedelta(seconds=lease_seconds)
            job.next_retry_at = None
            job.error_code = None
            job.error_detail = None
            job.worker_phase = None
            job.worker_heartbeat_at = None
            if restore and job.dismissed_at is not None:
                job.dismissed_at = None
                session.add(JobEvent(
                    job_id=job.id,
                    event="admin_restored",
                    detail={"expired_assignment_ids": [], "retry": True},
                ))
            if handoff_recovery and expected_provider_id and not job.runpod_job_id:
                job.runpod_job_id = expected_provider_id
            job.updated_at = now
            if handoff_recovery:
                recovered_detail: dict[str, Any] = {
                    "idempotency_key": expected_dispatch_key,
                    "source": "handoff.json",
                }
                if expected_provider_id:
                    recovered_detail["runpod_job_id"] = expected_provider_id
                session.add(JobEvent(
                    job_id=job.id,
                    event="runpod_dispatch_recovered",
                    detail=recovered_detail,
                ))
            session.add(JobEvent(job_id=job.id, event="publication_recovery_started", detail={
                "recovery_id": str(recovery_id), "worker": worker_id,
                "provider_job_id": expected_provider_id,
                "dispatch_key": expected_dispatch_key,
                "input_checksum": expected_checksum,
                "previous_failure_event_id": failure.id,
                "from": "failed", "to": "verifying",
            }))
            if admin_retry:
                session.add(JobEvent(
                    job_id=job.id,
                    event="admin_retry_started",
                    detail={
                        "previous_failure_code": previous_code,
                        "resume_stage": JobStage.verifying.value,
                        "reason": "reuse the selected separation package",
                        "recovery_id": str(recovery_id),
                        "reservation_resolution": reservation_resolution,
                        "dispatch_key": expected_dispatch_key,
                    },
                ))
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
            job.worker_phase = None
            job.worker_heartbeat_at = None
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

    async def get_many(self, track_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, Track]:
        """Batch track joins for administrator job rows."""
        ids = list(dict.fromkeys(track_ids))
        if not ids:
            return {}
        async with self._sf() as session:
            res = await session.execute(select(Track).where(Track.id.in_(ids)))
            return {track.id: track for track in res.scalars()}

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

    async def list_for_admin(
        self, *, deleted: str | None = None
    ) -> list[tuple[Track, uuid.UUID | None]]:
        """Admin library rows with the originating job, if one exists."""
        if deleted not in (None, "include", "only"):
            raise ValueError("deleted must be include or only")
        statement = select(Track, Job.id).outerjoin(Job, Job.track_id == Track.id)
        if deleted == "only":
            statement = statement.where(Track.deleted_at.is_not(None))
        elif deleted != "include":
            statement = statement.where(Track.deleted_at.is_(None))
        async with self._sf() as session:
            result = await session.execute(
                statement.order_by(Track.created_at.desc(), Track.id)
            )
            return [(track, job_id) for track, job_id in result.all()]

    async def set_deleted_for_admin(self, track_id: uuid.UUID, *, deleted: bool) -> Track:
        """Soft remove/restore and commit the generation-ledger event together."""
        async with self._sf() as session, session.begin():
            track = await session.get(Track, track_id, with_for_update=True)
            if track is None or (track.deleted_at is not None) == deleted:
                raise LookupError("track is absent or already in the requested state")
            track.deleted_at = utcnow() if deleted else None
            session.add(TrackGenerationEvent(
                track_id=track.id,
                event="admin_removed" if deleted else "admin_restored",
                from_generation=track.generation,
                to_generation=track.generation,
                detail={"s3_objects_deleted": False},
            ))
            return track

    async def remove_nonkeepers_for_admin(
        self, track_ids: Iterable[uuid.UUID], *, keeper_id: uuid.UUID
    ) -> list[uuid.UUID]:
        """Lock and validate the keeper before soft-removing alternatives."""
        ids = sorted(set(track_ids), key=str)
        async with self._sf() as session, session.begin():
            tracks = list((await session.scalars(
                select(Track).where(Track.id.in_(ids)).order_by(Track.id).with_for_update()
            )).all()) if ids else []
            by_id = {track.id: track for track in tracks}
            keeper = by_id.get(keeper_id)
            if keeper is None or keeper.deleted_at is not None:
                raise LookupError("keeper is missing or already removed")
            keeper_job = await session.scalar(select(Job).where(Job.track_id == keeper_id))
            if keeper_job is not None and keeper_job.status != JobStage.ready:
                raise LookupError("keeper is no longer ready")
            removed: list[uuid.UUID] = []
            now = utcnow()
            for track in tracks:
                if track.id == keeper_id or track.deleted_at is not None:
                    continue
                track.deleted_at = now
                session.add(TrackGenerationEvent(
                    track_id=track.id,
                    event="admin_removed",
                    from_generation=track.generation,
                    to_generation=track.generation,
                    detail={"s3_objects_deleted": False, "keeper_id": str(keeper_id)},
                ))
                removed.append(track.id)
            return removed


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


class NodeRepository:
    """Worker-node registry and assignments (docs/WORKER-NODES.md).

    Assignments are created only by the orchestrator while it owns the job's
    lease (B16) and are reported against by exactly the node they name. The
    repository never trusts a report as proof of work: the orchestrator
    re-checks the S3 markers before advancing, as it does for RunPod.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    # --- registry ------------------------------------------------------------

    async def heartbeat(
        self,
        node_id: str,
        *,
        display_name: str | None = None,
        capabilities: dict[str, Any] | None = None,
        agent_version: str | None = None,
        busy_assignment_id: uuid.UUID | None = None,
    ) -> Node:
        """Upsert the node row from an authenticated heartbeat."""
        now = utcnow()
        async with self._sf() as session, session.begin():
            # Lock order everywhere: assignment, then node (expire, reject and
            # the node-side reports do the same), so two writers for one node
            # cannot deadlock.
            held: NodeAssignment | None = None
            if busy_assignment_id is not None:
                held = await session.get(NodeAssignment, busy_assignment_id, with_for_update=True)
            node = await session.get(Node, node_id, with_for_update=True)
            if node is None:
                node = Node(id=node_id, display_name=display_name or node_id)
                session.add(node)
            if display_name:
                node.display_name = display_name
            if capabilities is not None:
                node.capabilities = capabilities
            if agent_version is not None:
                node.agent_version = agent_version[:64]
            # The node's own view of what it holds. A node that restarted and
            # reports no busy assignment while the server still records one
            # keeps the server's record: the stall watchdog decides, not the
            # heartbeat, so a brief restart cannot free a slot mid-job.
            # Only an open assignment held by this node counts: after the
            # watchdog expired one, the agent's next heartbeat must not re-mark
            # the node busy with a dead id that nothing would ever clear.
            if held is not None and held.node_id == node_id and held.status in NODE_ASSIGNMENT_OPEN:
                if node.busy_assignment_id is None:
                    node.busy_assignment_id = held.id
                if node.busy_assignment_id == held.id:
                    # The node holding it is alive: a long ffmpeg or demucs
                    # run with no progress line is not a stall (B16).
                    held.heartbeat_at = now
                    held.updated_at = now
            node.last_seen_at = now
            node.updated_at = now
            return node

    async def get(self, node_id: str) -> Node | None:
        async with self._sf() as session:
            return await session.get(Node, node_id)

    async def list_nodes(self) -> list[Node]:
        async with self._sf() as session:
            res = await session.execute(select(Node).order_by(Node.id))
            return list(res.scalars().all())

    async def set_disabled(self, node_id: str, disabled: bool) -> bool:
        async with self._sf() as session, session.begin():
            node = await session.get(Node, node_id, with_for_update=True)
            if node is None:
                return False
            node.disabled_at = utcnow() if disabled else None
            node.updated_at = utcnow()
            return True

    async def set_separation_disabled(self, node_id: str, disabled: bool) -> bool:
        """Toggle only new stem separation; downloads remain eligible (G5)."""
        async with self._sf() as session, session.begin():
            node = await session.get(Node, node_id, with_for_update=True)
            if node is None:
                return False
            node.separation_disabled_at = utcnow() if disabled else None
            node.updated_at = utcnow()
            return True

    async def available(
        self,
        kind: NodeAssignmentKind,
        *,
        online_seconds: float,
        now: datetime | None = None,
    ) -> list[Node]:
        """Online, enabled, idle nodes advertising ``kind``, least recently heard from first."""
        now = now or utcnow()
        cutoff = now - timedelta(seconds=online_seconds)
        async with self._sf() as session:
            res = await session.execute(
                select(Node)
                .where(
                    Node.disabled_at.is_(None),
                    Node.busy_assignment_id.is_(None),
                    Node.last_seen_at.is_not(None),
                    Node.last_seen_at >= cutoff,
                )
                # Least recently heard from first (updated_at moves on every
                # heartbeat), then id: a stable spread, not a strict round-robin.
                .order_by(Node.updated_at, Node.id)
            )
            nodes = list(res.scalars().all())
        return [
            node
            for node in nodes
            if (
                isinstance(node.capabilities, dict)
                and bool(node.capabilities.get(kind.value))
                and (
                    kind != NodeAssignmentKind.separate
                    or node.separation_disabled_at is None
                )
            )
        ]

    # --- assignments: orchestrator side -------------------------------------

    async def create_assignment(
        self,
        job_id: uuid.UUID,
        *,
        worker_id: str,
        node_id: str,
        kind: NodeAssignmentKind,
        payload: dict[str, Any],
        idempotency_key: str | None = None,
    ) -> NodeAssignment | None:
        """Hand one unit of work to one idle node, inside the job's lease (B16).

        Returns None when the node is no longer idle/enabled (a peer took it
        between the availability read and this locked write). Marks the node
        busy and records ``jobs.executor`` in the same transaction.
        """
        now = utcnow()
        async with self._sf() as session, session.begin():
            job = await session.get(Job, job_id, with_for_update=True)
            if job is None:
                raise InvalidTransition(job_id, JobStage.failed, JobStage.downloading)
            _require_lease(job, worker_id, job.status, now)
            node = await session.get(Node, node_id, with_for_update=True)
            if (
                node is None or node.disabled_at is not None
                or node.busy_assignment_id is not None
                or (
                    kind == NodeAssignmentKind.separate
                    and node.separation_disabled_at is not None
                )
            ):
                return None
            assignment = NodeAssignment(
                job_id=job_id,
                node_id=node_id,
                kind=kind,
                status=NodeAssignmentStatus.assigned,
                idempotency_key=idempotency_key,
                payload=payload,
                heartbeat_at=now,
            )
            session.add(assignment)
            await session.flush()
            node.busy_assignment_id = assignment.id
            node.updated_at = now
            job.executor = f"node:{node_id}"
            job.updated_at = now
            session.add(
                JobEvent(
                    job_id=job_id,
                    event="node_assigned",
                    detail={
                        "assignment_id": str(assignment.id),
                        "node_id": node_id,
                        "kind": kind.value,
                        "idempotency_key": idempotency_key,
                        "attempt": (assignment.payload or {}).get("attempt"),
                    },
                )
            )
            return assignment

    async def get_assignment(self, assignment_id: uuid.UUID) -> NodeAssignment | None:
        async with self._sf() as session:
            return await session.get(NodeAssignment, assignment_id)

    async def latest_for_job(
        self, job_id: uuid.UUID, kind: NodeAssignmentKind | None = None
    ) -> NodeAssignment | None:
        async with self._sf() as session:
            stmt = select(NodeAssignment).where(NodeAssignment.job_id == job_id)
            if kind is not None:
                stmt = stmt.where(NodeAssignment.kind == kind)
            stmt = stmt.order_by(NodeAssignment.created_at.desc(), NodeAssignment.id.desc()).limit(1)
            return (await session.execute(stmt)).scalar_one_or_none()

    async def list_for_job(self, job_id: uuid.UUID) -> list[NodeAssignment]:
        async with self._sf() as session:
            res = await session.execute(
                select(NodeAssignment)
                .where(NodeAssignment.job_id == job_id)
                .order_by(NodeAssignment.created_at, NodeAssignment.id)
            )
            return list(res.scalars().all())

    async def list_for_jobs(
        self, job_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, list[NodeAssignment]]:
        """Batch assignment histories for administrator job rows."""
        ids = list(dict.fromkeys(job_ids))
        if not ids:
            return {}
        async with self._sf() as session:
            res = await session.execute(
                select(NodeAssignment)
                .where(NodeAssignment.job_id.in_(ids))
                .order_by(NodeAssignment.job_id, NodeAssignment.created_at, NodeAssignment.id)
            )
            grouped: dict[uuid.UUID, list[NodeAssignment]] = {job_id: [] for job_id in ids}
            for assignment in res.scalars():
                grouped[assignment.job_id].append(assignment)
            return grouped

    async def expire(
        self, assignment_id: uuid.UUID, *, reason: str, worker_id: str | None = None
    ) -> bool:
        """Orchestrator gave up on a silent node: close the assignment, free the node.

        With ``worker_id`` the write is fenced to the job's lease (B2): a
        handler that lost its lease cannot expire an assignment a newer owner
        may be relying on. Returns False when the fence fails.
        """
        now = utcnow()
        async with self._sf() as session, session.begin():
            assignment = await session.get(NodeAssignment, assignment_id, with_for_update=True)
            if assignment is None or assignment.status not in NODE_ASSIGNMENT_OPEN:
                return False
            if worker_id is not None:
                job = await session.get(Job, assignment.job_id, with_for_update=True)
                if job is None or not _lease_is_owned(job, worker_id, now):
                    return False
            assignment.status = NodeAssignmentStatus.expired
            assignment.detail = {**(assignment.detail or {}), "expired_reason": reason}
            assignment.updated_at = now
            await self._free_node(session, assignment, now)
            return True

    async def release_terminal_assignment(
        self, assignment_id: uuid.UUID, *, reason: str
    ) -> NodeAssignment | None:
        """Expire terminal-job work and write its admin audit atomically.

        The job is re-checked under lock so a live-job race cannot turn the
        administrator's release into an unfenced assignment expiry. Any audit
        insert failure rolls back both the assignment and node-pointer changes.
        """
        now = utcnow()
        async with self._sf() as session, session.begin():
            assignment = await session.get(NodeAssignment, assignment_id, with_for_update=True)
            if assignment is None or assignment.status not in NODE_ASSIGNMENT_OPEN:
                return None
            job = await session.get(Job, assignment.job_id, with_for_update=True)
            if job is None or job.status not in TERMINAL_STAGES:
                return None
            assignment.status = NodeAssignmentStatus.expired
            assignment.detail = {**(assignment.detail or {}), "expired_reason": reason}
            assignment.updated_at = now
            await self._free_node(session, assignment, now)
            session.add(
                JobEvent(
                    job_id=job.id,
                    event="admin_assignment_released",
                    detail={
                        "assignment_id": str(assignment.id),
                        "node_id": assignment.node_id,
                        "kind": assignment.kind.value,
                        "reason": reason,
                    },
                )
            )
            return assignment

    async def reject(
        self, assignment_id: uuid.UUID, *, reason: str, worker_id: str | None = None
    ) -> bool:
        """Orchestrator refused what a node reported complete (e.g. a receipt
        that fails A12): mark it failed so the retry assigns afresh, free the
        node. Lease-fenced like ``expire`` when ``worker_id`` is given."""
        now = utcnow()
        async with self._sf() as session, session.begin():
            assignment = await session.get(NodeAssignment, assignment_id, with_for_update=True)
            if assignment is None or assignment.status in (
                NodeAssignmentStatus.failed, NodeAssignmentStatus.expired
            ):
                return False
            if worker_id is not None:
                job = await session.get(Job, assignment.job_id, with_for_update=True)
                if job is None or not _lease_is_owned(job, worker_id, now):
                    return False
            assignment.status = NodeAssignmentStatus.failed
            assignment.detail = {**(assignment.detail or {}), "rejected_reason": reason}
            assignment.updated_at = now
            await self._free_node(session, assignment, now)
            return True

    # --- assignments: node side ---------------------------------------------

    async def next_for_node(self, node_id: str) -> NodeAssignment | None:
        """The oldest assignment waiting for this node, or None.

        A disabled node (operator kill switch) is offered nothing, including
        work created before it was disabled; the watchdog expires that work
        and the orchestrator redispatches it elsewhere.
        """
        async with self._sf() as session:
            node = await session.get(Node, node_id)
            if node is not None and node.disabled_at is not None:
                return None
            res = await session.execute(
                select(NodeAssignment)
                .where(
                    NodeAssignment.node_id == node_id,
                    NodeAssignment.status.in_(
                        (NodeAssignmentStatus.assigned, NodeAssignmentStatus.acknowledged)
                    ),
                )
                .order_by(NodeAssignment.created_at, NodeAssignment.id)
                .limit(1)
            )
            return res.scalar_one_or_none()

    async def acknowledge(self, assignment_id: uuid.UUID, *, node_id: str) -> bool:
        return await self._node_update(
            assignment_id,
            node_id,
            allowed=(NodeAssignmentStatus.assigned, NodeAssignmentStatus.acknowledged),
            status=NodeAssignmentStatus.acknowledged,
        )

    async def progress(self, assignment_id: uuid.UUID, *, node_id: str, phase: str) -> bool:
        return await self._node_update(
            assignment_id,
            node_id,
            allowed=tuple(NODE_ASSIGNMENT_OPEN),
            status=NodeAssignmentStatus.running,
            phase=phase[:256],
        )

    async def complete(
        self, assignment_id: uuid.UUID, *, node_id: str, detail: dict[str, Any] | None
    ) -> bool:
        return await self._node_update(
            assignment_id,
            node_id,
            allowed=tuple(NODE_ASSIGNMENT_OPEN),
            status=NodeAssignmentStatus.completed,
            detail=detail,
            terminal=True,
        )

    async def fail(
        self, assignment_id: uuid.UUID, *, node_id: str, detail: dict[str, Any] | None
    ) -> bool:
        return await self._node_update(
            assignment_id,
            node_id,
            allowed=tuple(NODE_ASSIGNMENT_OPEN),
            status=NodeAssignmentStatus.failed,
            detail=detail,
            terminal=True,
        )

    async def _node_update(
        self,
        assignment_id: uuid.UUID,
        node_id: str,
        *,
        allowed: tuple[NodeAssignmentStatus, ...],
        status: NodeAssignmentStatus,
        phase: str | None = None,
        detail: dict[str, Any] | None = None,
        terminal: bool = False,
    ) -> bool:
        """A node may only move its own open assignment; anything else is a no-op False."""
        now = utcnow()
        async with self._sf() as session, session.begin():
            assignment = await session.get(NodeAssignment, assignment_id, with_for_update=True)
            if (
                assignment is None
                or assignment.node_id != node_id
                or assignment.status not in allowed
            ):
                return False
            assignment.status = status
            assignment.heartbeat_at = now
            assignment.updated_at = now
            if phase is not None:
                assignment.phase = phase
            if detail is not None:
                assignment.detail = detail
            if terminal:
                await self._free_node(session, assignment, now)
            return True

    async def _free_node(
        self, session: AsyncSession, assignment: NodeAssignment, now: datetime
    ) -> None:
        node = await session.get(Node, assignment.node_id, with_for_update=True)
        if node is not None and node.busy_assignment_id == assignment.id:
            node.busy_assignment_id = None
            node.updated_at = now


class RuntimeSettingsRepository:
    """Operator runtime settings (admin page): DB overrides over env defaults.

    The orchestrator reads the effective value at each routing decision, so
    a change takes effect on the next pass without a restart. A missing row
    falls back to the environment ``Settings`` value — behaviour is unchanged
    until the owner changes something.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    async def get(self, key: str) -> RuntimeSetting | None:
        async with self._sf() as session:
            return await session.get(RuntimeSetting, key)

    async def get_all(self) -> dict[str, RuntimeSetting]:
        async with self._sf() as session:
            res = await session.execute(select(RuntimeSetting))
            return {row.key: row for row in res.scalars().all()}

    async def set_value(self, key: str, value: Any) -> RuntimeSetting:
        """Atomically upsert one setting, including concurrent first writes."""
        async with self._sf() as session, session.begin():
            bind = session.bind
            dialect = bind.dialect.name if bind is not None else ""
            now = utcnow()
            values = {"key": key, "value": value, "updated_at": now}
            if dialect == "postgresql":
                pg_statement = postgresql_insert(RuntimeSetting).values(
                    **values
                ).on_conflict_do_update(
                    index_elements=[RuntimeSetting.key],
                    set_={"value": value, "updated_at": now},
                ).returning(RuntimeSetting)
                row: RuntimeSetting = (await session.execute(pg_statement)).scalar_one()
            elif dialect == "sqlite":
                sqlite_statement = sqlite_insert(RuntimeSetting).values(
                    **values
                ).on_conflict_do_update(
                    index_elements=[RuntimeSetting.key],
                    set_={"value": value, "updated_at": now},
                ).returning(RuntimeSetting)
                row = (await session.execute(sqlite_statement)).scalar_one()
            else:
                raise RuntimeError(f"unsupported runtime-settings dialect: {dialect}")
            return row

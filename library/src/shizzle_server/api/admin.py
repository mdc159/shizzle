"""Administrator API: job tracing, discrepancy evidence, runtime controls.

Every route here is behind ``require_admin``: the owner's Tailscale-only
reverse proxy adds the ``X-Shizzle-Admin`` header (the shared
``SHIZZLE_ADMIN_PROXY_SECRET``); the public proxy strips it, and an empty
secret means every admin route 404s — fail closed. The page is thus
reachable only from the Tailscale network (plus local dev/tests via
``SHIZZLE_ADMIN_OPEN``).

E2/E6: no route here ever returns ``source_ref`` (the raw submitted URL),
node assignment payloads (which carry it), node tokens, S3 credentials,
signed URLs or the admin secret itself.

The evidence model follows the job-trace research: for each expected
artifact/property, report ``expected / observed / proof / checkedAt`` with a
status of ``ok``, ``missing``, ``mismatch``, ``not_yet`` or ``unknown``.
Database checks always run; ``?verify=s3`` additionally HEADs the exact S3
keys (blocking calls off the event loop). An S3 error yields ``unknown``,
never ``ok`` or ``missing``. The page DETECTS the known pipeline defects
(job-trace §4A); fixing them is out of scope here.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import re
import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..db.models import (
    NODE_ASSIGNMENT_OPEN,
    RUNNABLE_STAGES,
    TERMINAL_STAGES,
    Job,
    JobEvent,
    JobStage,
    NodeAssignment,
    SourceType,
    Track,
    utcnow,
)
from ..db.repository import (
    JobRepository,
    NodeRepository,
    RuntimeSettingsRepository,
    TrackRepository,
    pending_runpod_dispatch,
    track_id_for_job,
)
from ..orchestrator import cloud
from ..orchestrator import nodes as node_routing
from ..orchestrator.runtime import (
    NODE_ROUTING_KEY,
    RUNPOD_MAX_CONCURRENT_KEY,
    normalize_routing,
    parse_runpod_limit,
    validate_setting,
)
from ..settings import Settings
from .media import s3_client

router = APIRouter(prefix="/api/admin", tags=["admin"])

STEM_ROLES = ("vocals", "drums", "bass", "guitar", "piano", "shizzle")
ADMIN_HEADER = "x-shizzle-admin"


# --- auth (fail closed) --------------------------------------------------------


def require_admin(request: Request) -> None:
    """The Tailscale-only proxy adds the shared secret header; nobody else.

    When ``SHIZZLE_ADMIN_PROXY_SECRET`` is empty every admin route 404s:
    fail closed (an unconfigured admin surface must not silently open).
    ``SHIZZLE_ADMIN_OPEN=1`` skips the check for local dev and tests only.
    """
    settings: Settings = request.app.state.settings
    if settings.shizzle_admin_open:
        return
    secret = settings.shizzle_admin_proxy_secret
    presented = request.headers.get(ADMIN_HEADER, "")
    if (
        not secret
        or not hmac.compare_digest(presented.encode("utf-8"), secret.encode("utf-8"))
    ):
        raise HTTPException(status_code=404, detail="Not Found")


Admin = Depends(require_admin)


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _jobs(request: Request) -> JobRepository:
    repo: JobRepository = request.app.state.job_repo
    return repo


def _tracks(request: Request) -> TrackRepository:
    repo: TrackRepository = request.app.state.track_repo
    return repo


def _node_repo(request: Request) -> NodeRepository:
    repo: NodeRepository = request.app.state.node_repo
    return repo


def _runtime(request: Request) -> RuntimeSettingsRepository:
    repo: RuntimeSettingsRepository = request.app.state.runtime_repo
    return repo


# --- schemas -------------------------------------------------------------------


EvidenceStatus = Literal["ok", "missing", "mismatch", "not_yet", "unknown"]


class EvidenceItem(BaseModel):
    stage: str
    name: str
    expected: str
    observed: str
    proof: str
    checkedAt: datetime
    status: EvidenceStatus


class AdminJobRow(BaseModel):
    """A job list row: failure diagnostics, control-plane state, flags."""

    jobId: str
    status: str
    stage: str
    sourceType: str
    title: str | None = None
    artist: str = ""
    attempt: int
    executor: str | None = None
    runpodJobId: str | None = None
    workerPhase: str | None = None
    workerHeartbeatAt: datetime | None = None
    errorCode: str | None = None
    errorDetail: str | None = None
    leaseOwner: str | None = None
    leaseExpiresAt: datetime | None = None
    nextRetryAt: datetime | None = None
    trackId: str | None = None
    createdAt: datetime | None = None
    updatedAt: datetime | None = None
    flags: list[str] = Field(default_factory=list)

    @classmethod
    def from_job(cls, job: Job, flags: list[str]) -> AdminJobRow:
        return cls(
            jobId=job.id.hex,
            status=job.status.value,
            stage=job.status.value,
            sourceType=job.source_type.value,
            title=_redact_value(job.title),
            artist=_redact_value(job.artist),
            attempt=job.attempt,
            executor=job.executor,
            runpodJobId=job.runpod_job_id,
            workerPhase=_redact_value(job.worker_phase),
            workerHeartbeatAt=job.worker_heartbeat_at,
            errorCode=job.error_code,
            errorDetail=_redact_value(job.error_detail),
            leaseOwner=job.lease_owner,
            leaseExpiresAt=job.lease_expires_at,
            nextRetryAt=job.next_retry_at,
            trackId=str(job.track_id) if job.track_id else None,
            createdAt=job.created_at,
            updatedAt=job.updated_at,
            flags=flags,
        )


class AdminJobListResponse(BaseModel):
    jobs: list[AdminJobRow]
    total: int
    limit: int
    offset: int
    counts: dict[str, int]


class AdminAssignmentInfo(BaseModel):
    """An assignment row without its payload (E2: payloads carry source URLs)."""

    id: str
    nodeId: str
    kind: str
    status: str
    idempotencyKey: str | None = None
    phase: str | None = None
    heartbeatAt: datetime | None = None
    detail: dict[str, Any] | None = None
    createdAt: datetime
    updatedAt: datetime

    @classmethod
    def from_assignment(cls, a: NodeAssignment) -> AdminAssignmentInfo:
        return cls(
            id=str(a.id),
            nodeId=a.node_id,
            kind=a.kind.value,
            status=a.status.value,
            idempotencyKey=a.idempotency_key,
            phase=_redact_value(a.phase),
            heartbeatAt=a.heartbeat_at,
            detail=_redact_value(dict(a.detail or {})),
            createdAt=a.created_at,
            updatedAt=a.updated_at,
        )


class AdminJobColumns(BaseModel):
    """Every job column except source_ref (E2)."""

    jobId: str
    sourceType: str
    title: str | None = None
    artist: str = ""
    status: str
    runpodJobId: str | None = None
    executor: str | None = None
    workerPhase: str | None = None
    workerHeartbeatAt: datetime | None = None
    attempt: int
    idempotencyKey: str
    leaseOwner: str | None = None
    leaseExpiresAt: datetime | None = None
    nextRetryAt: datetime | None = None
    profileVersion: int
    inputChecksum: str | None = None
    outputChecksums: dict[str, Any] | None = None
    errorCode: str | None = None
    errorDetail: str | None = None
    stageTimings: dict[str, Any] | None = None
    trackId: str | None = None
    createdAt: datetime
    updatedAt: datetime

    @classmethod
    def from_job(cls, job: Job) -> AdminJobColumns:
        return cls(
            jobId=job.id.hex,
            sourceType=job.source_type.value,
            title=_redact_value(job.title),
            artist=_redact_value(job.artist),
            status=job.status.value,
            runpodJobId=job.runpod_job_id,
            executor=job.executor,
            workerPhase=_redact_value(job.worker_phase),
            workerHeartbeatAt=job.worker_heartbeat_at,
            attempt=job.attempt,
            idempotencyKey=job.idempotency_key,
            leaseOwner=job.lease_owner,
            leaseExpiresAt=job.lease_expires_at,
            nextRetryAt=job.next_retry_at,
            profileVersion=job.profile_version,
            inputChecksum=job.input_checksum,
            outputChecksums=_redact_value(job.output_checksums),
            errorCode=job.error_code,
            errorDetail=_redact_value(job.error_detail),
            stageTimings=job.stage_timings,
            trackId=str(job.track_id) if job.track_id else None,
            createdAt=job.created_at,
            updatedAt=job.updated_at,
        )


class AdminEventInfo(BaseModel):
    id: int
    event: str
    detail: dict[str, Any] | None = None
    createdAt: datetime


class AdminTrackInfo(BaseModel):
    id: str
    title: str
    artist: str
    durationSeconds: float
    s3Prefix: str
    generation: int
    manifestKey: str
    integrity: dict[str, Any] | None = None
    deletedAt: datetime | None = None
    createdAt: datetime


class DispatchAttempt(BaseModel):
    idempotencyKey: str | None = None
    attemptPrefix: str | None = None
    runpodJobId: str | None = None


class AdminJobDetailResponse(BaseModel):
    job: AdminJobColumns
    derivedTrackId: str
    dispatchAttempt: DispatchAttempt
    events: list[AdminEventInfo]
    assignments: list[AdminAssignmentInfo]
    track: AdminTrackInfo | None = None
    evidence: list[EvidenceItem]


class AdminCurrentAssignment(BaseModel):
    id: str
    jobId: str
    kind: str
    status: str
    phase: str | None = None
    heartbeatAt: datetime | None = None
    createdAt: datetime


class AdminNodeInfo(BaseModel):
    id: str
    displayName: str
    online: bool
    enabled: bool
    capabilities: dict[str, Any]
    agentVersion: str | None = None
    lastSeenAt: datetime | None = None
    busyAssignmentId: str | None = None
    currentAssignment: AdminCurrentAssignment | None = None


class AdminNodeListResponse(BaseModel):
    nodes: list[AdminNodeInfo]
    onlineSeconds: float


class AdminSettingValue(BaseModel):
    value: Any = None
    source: Literal["database", "environment"]
    error: str | None = None


class AdminSettingsResponse(BaseModel):
    nodeRouting: AdminSettingValue
    runpodMaxConcurrent: AdminSettingValue
    runpodConfigured: bool
    jobsAtRunpod: int


class AdminSettingsUpdate(BaseModel):
    nodeRouting: str | None = Field(default=None, max_length=32)
    runpodMaxConcurrent: int | None = Field(default=None, ge=0)


class AdminReleaseResponse(BaseModel):
    ok: bool
    assignmentId: str
    nodeId: str
    jobId: str


# --- helpers -------------------------------------------------------------------


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _age_seconds(since: datetime | None) -> float | None:
    aware = _aware(since)
    if aware is None:
        return None
    return (utcnow() - aware).total_seconds()


def _event_detail(event: JobEvent) -> dict[str, Any]:
    return event.detail if isinstance(event.detail, dict) else {}


# E2/E3 defense in depth for the admin surface: nested job/assignment details
# and diagnostic strings can echo values the pipeline stored — e.g. a
# URL-download completion event carries detail.source.source_ref, and node
# failure text can quote the submitted URL. Nothing credential-shaped or
# URL-shaped leaves an admin route, whatever the database happens to hold.
_REDACTED = "[redacted]"
_REDACT_KEY_FRAGMENTS = (
    "source_ref",
    "authorization",
    "cookie",
    "password",
    "passcode",
    "token",
    "secret",
    "url",
)
_URL_SCRUB_RE = re.compile(r"https?://[^\s\"'<>]+")


def _redact_value(value: Any) -> Any:
    """Recursively redact credential-shaped keys and scrub URLs from strings."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            lowered = str(key).lower()
            if any(fragment in lowered for fragment in _REDACT_KEY_FRAGMENTS):
                redacted[key] = _REDACTED
            else:
                redacted[key] = _redact_value(child)
        return redacted
    if isinstance(value, list):
        return [_redact_value(child) for child in value]
    if isinstance(value, str):
        return _URL_SCRUB_RE.sub(_REDACTED, value)
    return value


def _stage_entry_age(job: Job, events: list[JobEvent]) -> float | None:
    """Seconds since the job entered its current stage (approximate, DB-only)."""
    for event in reversed(events):
        detail = _event_detail(event)
        if event.event == "stage_completed" and detail.get("to") == job.status.value:
            return _age_seconds(event.created_at)
    return _age_seconds(job.created_at)


# Generous "no meaningful event for this long means something is wrong" bounds
# per stage (research job-trace §4C: updated_at and /health alone prove
# nothing; these flag silence, not slowness).
_STAGE_QUIET_BOUNDS = {
    "pending": 300.0,
    "downloading_upload": 600.0,
    "splitting": 3600.0,
    "verifying": 3600.0,
    "publishing": 7200.0,
}


def _last_meaningful_event_age(events: list[JobEvent]) -> float | None:
    """Age of the newest event that is not a bounded heartbeat mirror.

    ``worker_progress`` writes only on phase change (B8) but is mirrored by
    polling loops; every other event is a routing or stage decision.
    """
    for event in reversed(events):
        if event.event != "worker_progress":
            return _age_seconds(event.created_at)
    return None


def _selected_package_prefix(job: Job, events: list[JobEvent], stages: set[str]) -> str | None:
    """The attempt prefix the dispatched->verifying transition selected.

    Falls back to the current attempt's prefix (what a fresh dispatch would
    use) when the transition has not happened yet.
    """
    track_id = track_id_for_job(job.id)
    base = cloud.separation_prefix(track_id)
    for event in reversed(events):
        detail = _event_detail(event)
        if (
            event.event == "stage_completed"
            and detail.get("from") == "dispatched"
            and detail.get("to") == "verifying"
        ):
            prefix = detail.get("package_prefix")
            if isinstance(prefix, str) and prefix.startswith(f"{base}/attempts/"):
                return prefix
            break
    if "dispatched" in stages:
        return cloud.attempt_prefix(track_id, f"{job.id.hex}:{job.attempt}")
    return None


def _selected_dispatch_key(job: Job, events: list[JobEvent]) -> str | None:
    """The idempotency key of the dispatch the job's CURRENT identity rests on,
    derived from durable dispatch history — never from ``jobs.attempt``.

    Later-stage retries (verification, publication) increment the retry
    counter WITHOUT redispatching: the selected package still belongs to the
    dispatch that was confirmed before the dispatched->verifying transition.
    A job still inside ``dispatched`` names its in-flight reservation or
    confirmation when one exists, else the key a fresh dispatch would use.
    """
    transition_index = None
    for index, event in enumerate(events):
        detail = _event_detail(event)
        if (
            event.event == "stage_completed"
            and detail.get("from") == "dispatched"
            and detail.get("to") == "verifying"
        ):
            transition_index = index
    if transition_index is not None:
        for event in reversed(events[:transition_index]):
            key = _event_detail(event).get("idempotency_key")
            if event.event == "runpod_dispatched" and isinstance(key, str):
                return key
        return None  # legacy transition with no keyed confirmation
    pending = pending_runpod_dispatch(events)
    if pending is not None:
        key = _event_detail(pending).get("idempotency_key")
        if isinstance(key, str):
            return key
    for event in reversed(events):
        key = _event_detail(event).get("idempotency_key")
        if event.event == "runpod_dispatched" and isinstance(key, str):
            return key
    return f"{job.id.hex}:{job.attempt}"


def _stages_reached(job: Job, events: list[JobEvent]) -> set[str]:
    """Every pipeline stage this job verifiably reached, from durable history.

    Derived from ``stage_completed`` transitions and the ``failed`` event's
    originating stage — never from the job's current status alone, because a
    FAILED job must not erase the stages it already passed (a job that died
    in verifying still transferred its source and dispatched its attempt).
    """
    reached = {"pending"}
    for event in events:
        detail = _event_detail(event)
        if event.event == "stage_completed" and isinstance(detail.get("to"), str):
            reached.add(detail["to"])
        elif event.event == "failed" and isinstance(detail.get("stage"), str):
            reached.add(detail["stage"])
    reached.add(job.status.value)
    return reached


# --- discrepancy evidence (database-only; always runs) --------------------------


class _Evidence:
    def __init__(self) -> None:
        self.items: list[EvidenceItem] = []
        self.flags: list[str] = []

    def add(
        self,
        stage: str,
        name: str,
        *,
        expected: str,
        observed: str,
        proof: str,
        status: EvidenceStatus,
        flag: str | None = None,
    ) -> None:
        self.items.append(
            EvidenceItem(
                stage=stage,
                name=name,
                # Diagnostic strings can quote stored URLs (node error text,
                # S3 error messages): scrubbed like every other admin string.
                expected=_redact_value(expected),
                observed=_redact_value(observed),
                proof=_redact_value(proof),
                checkedAt=utcnow(),
                status=status,
            )
        )
        if flag is not None and flag not in self.flags:
            self.flags.append(flag)


def _build_db_evidence(
    job: Job,
    events: list[JobEvent],
    assignments: list[NodeAssignment],
    busy_pointers: dict[str, str],  # assignment id (hex) -> node id
    track: Track | None,
    settings: Settings,
    stages: set[str] | None = None,
) -> _Evidence:
    ev = _Evidence()
    now = utcnow()
    stages = stages if stages is not None else _stages_reached(job, events)

    # 1. Ingest: the created event is the start of the trace.
    created = next((e for e in events if e.event == "created"), None)
    ev.add(
        "pending",
        "created_event",
        expected="a created event records ingest",
        observed="present" if created else "absent",
        proof="job_events",
        status="ok" if created else "missing",
    )

    # 2. Dispatch identity: the selected dispatch's durable key, compared
    # against what history confirms. The RETRY COUNTER (jobs.attempt) is a
    # separate fact: later-stage retries increment it without redispatching,
    # so it must never be presented as the dispatch identity.
    pending = pending_runpod_dispatch(events)
    confirmed = None
    for event in reversed(events):
        detail = _event_detail(event)
        if event.event == "runpod_dispatched" and isinstance(
            detail.get("idempotency_key"), str
        ):
            confirmed = detail
            break
    selected_key = _selected_dispatch_key(job, events)
    current_retry_key = f"{job.id.hex}:{job.attempt}"
    if "dispatched" not in stages:
        observed = "job has not reached dispatched"
        status: EvidenceStatus = "not_yet"
    elif confirmed is not None:
        confirmed_key = str(confirmed.get("idempotency_key"))
        observed = (
            f"confirmed runpod_job_id={confirmed.get('runpod_job_id')} "
            f"key={confirmed_key}"
        )
        if confirmed_key == selected_key:
            status = "ok"
        elif job.worker_phase == "failed":
            # B12: the confirmed execution is closed and the retry dispatches
            # fresh — consistent by design, not a contradiction.
            observed += f"; worker failed, retry dispatches fresh under {selected_key}"
            status = "ok"
        else:
            observed += f"; expected the selected dispatch {selected_key}"
            status = "mismatch"
    elif pending is not None:
        pending_key = str(_event_detail(pending).get("idempotency_key"))
        observed = f"reservation key={pending_key} unconfirmed"
        status = "ok" if pending_key == selected_key else "mismatch"
    else:
        observed = "no dispatch recorded yet (waiting for a node or the RunPod cap)"
        status = "not_yet"
    expected = (
        f"the selected dispatch key {selected_key} "
        f"(retry counter now at {current_retry_key})"
        if selected_key is not None
        else "a keyed dispatch selects the package"
    )
    ev.add(
        "dispatched",
        "dispatch_identity",
        expected=expected,
        observed=observed,
        proof="job_events (runpod_dispatch_started / runpod_dispatched)",
        status=status,
    )

    # 3. Unresolved dispatch reservation beyond the fail-closed window (B10).
    if pending is not None:
        pending_for = _age_seconds(pending.created_at) or 0.0
        fail_closed_after = (
            max(
                settings.runpod_queue_timeout_seconds,
                settings.runpod_cold_start_seconds,
            )
            + settings.runpod_worker_stall_seconds
        )
        if pending_for > fail_closed_after:
            ev.add(
                "dispatched",
                "dispatch_reservation",
                expected=(
                    "a reservation resolves to a receipt or package within "
                    f"{fail_closed_after:.0f}s (B10)"
                ),
                observed=f"unresolved for {pending_for:.0f}s",
                proof="job_events",
                status="mismatch",
                flag="unresolved_dispatch_reservation",
            )
        else:
            ev.add(
                "dispatched",
                "dispatch_reservation",
                expected="a reservation resolves within the fail-closed window",
                observed=f"unresolved for {pending_for:.0f}s (within window)",
                proof="job_events",
                status="ok",
            )
    else:
        ev.add(
            "dispatched",
            "dispatch_reservation",
            expected="no unresolved reservation",
            observed="none",
            proof="job_events",
            status="ok",
        )

    # 4. Lease: an expired lease on runnable work means nobody owns it now.
    lease_expires = _aware(job.lease_expires_at)
    if job.status in RUNNABLE_STAGES:
        expired = lease_expires is not None and lease_expires <= now
        ev.add(
            job.status.value,
            "lease",
            expected="a runnable job holds an unexpired lease (or none, between passes)",
            observed=(
                f"owner={job.lease_owner} expires={lease_expires.isoformat() if lease_expires else None}"
            ),
            proof="jobs.lease_owner / jobs.lease_expires_at",
            status="mismatch" if expired else "ok",
            flag="expired_lease_on_runnable" if expired else None,
        )

    # 5. Assignments: ack deadline, stall, absolute cap, terminal-job leaks,
    #    and busy pointers naming work that is not open.
    for a in assignments:
        label = f"{a.kind.value}_assignment_{str(a.id)[:8]}"
        stage = "downloading" if a.kind.value == "download" else "dispatched"
        if a.status in NODE_ASSIGNMENT_OPEN:
            if job.status in TERMINAL_STAGES:
                ev.add(
                    stage,
                    label,
                    expected="a terminal job has no open assignment",
                    observed=f"assignment {a.id} still {a.status.value} on {a.node_id}",
                    proof="node_assignments.status",
                    status="mismatch",
                    flag="open_assignment_on_terminal_job",
                )
            ack_age = _age_seconds(a.created_at) or 0.0
            if a.status.value == "assigned" and ack_age > settings.node_ack_seconds:
                ev.add(
                    stage,
                    f"{label}_ack",
                    expected=f"acknowledged within {settings.node_ack_seconds:.0f}s",
                    observed=f"assigned for {ack_age:.0f}s",
                    proof="node_assignments.created_at",
                    status="mismatch",
                    flag="assignment_ack_overdue",
                )
            beat_age = _age_seconds(a.heartbeat_at or a.updated_at)
            if beat_age is not None and beat_age > settings.node_stall_seconds:
                ev.add(
                    stage,
                    f"{label}_stall",
                    expected=f"a heartbeat within {settings.node_stall_seconds:.0f}s",
                    observed=f"no heartbeat for {beat_age:.0f}s",
                    proof="node_assignments.heartbeat_at",
                    status="mismatch",
                    flag="assignment_stalled",
                )
            total_age = _age_seconds(a.created_at) or 0.0
            if total_age > settings.node_assignment_max_seconds:
                ev.add(
                    stage,
                    f"{label}_cap",
                    expected=f"an assignment lives at most {settings.node_assignment_max_seconds:.0f}s",
                    observed=f"alive for {total_age:.0f}s",
                    proof="node_assignments.created_at",
                    status="mismatch",
                    flag="assignment_over_cap",
                )
        else:
            holder = busy_pointers.get(str(a.id))
            if holder is not None:
                ev.add(
                    "nodes",
                    f"{label}_busy_pointer",
                    expected="a node's busy pointer names open work",
                    observed=f"node {holder} is busy with {a.status.value} assignment {a.id}",
                    proof="nodes.busy_assignment_id",
                    status="mismatch",
                    flag="node_busy_pointer_stale",
                )

    # 6. An OUTSTANDING wait longer than its routing window. The alarm
    # describes a wait that is still holding the job back, not an old event:
    # anything after the wait that resolved it (an assignment, the RunPod
    # fallback, a stage transition, a retry's fresh outage window, or the
    # job's terminal completion) retires it, and a wait from an earlier
    # attempt never applies to the current one.
    for kind, window in (
        ("download", settings.node_download_wait_seconds),
        ("separate", settings.node_separate_wait_seconds),
    ):
        wait = None
        wait_index = None
        for index in range(len(events) - 1, -1, -1):
            detail = _event_detail(events[index])
            if events[index].event == "node_wait" and detail.get("kind") == kind:
                wait = events[index]
                wait_index = index
                break
        if wait is None:
            continue
        wait_detail = _event_detail(wait)
        assert wait_index is not None  # set together with wait
        resolving_after = any(
            event.event
            in (
                "node_assigned",
                "fallback_runpod",
                "runpod_dispatch_started",
                "runpod_dispatch_recovered",
                "stage_completed",
                "worker_completed",
                "track_published",
                "failed",
                "retry_scheduled",
                "node_expired",
                "node_failed",
            )
            for event in events[wait_index + 1 :]
        )
        same_attempt = wait_detail.get("attempt") in (None, job.attempt)
        outstanding = (
            job.status in RUNNABLE_STAGES and not resolving_after and same_attempt
        )
        waited = _age_seconds(wait.created_at) or 0.0
        if outstanding:
            stage = "downloading" if kind == "download" else "dispatched"
            if waited > window:
                ev.add(
                    stage,
                    f"{kind}_wait_window",
                    expected=f"the {kind} wait window is {window:.0f}s",
                    observed=(
                        f"still waiting after {waited:.0f}s "
                        f"(attempt {wait_detail.get('attempt')})"
                    ),
                    proof="job_events (node_wait, unresolved)",
                    status="mismatch",
                    flag="wait_over_window",
                )
            else:
                ev.add(
                    stage,
                    f"{kind}_wait_window",
                    expected=f"the {kind} wait window is {window:.0f}s",
                    observed=f"waiting for {waited:.0f}s (within window)",
                    proof="job_events (node_wait, unresolved)",
                    status="ok",
                )

    # 7. No meaningful event for the stage's wall-clock age.
    if job.status in RUNNABLE_STAGES:
        stage_age = _stage_entry_age(job, events)
        if job.status.value == "downloading":
            bound = (
                settings.node_download_wait_seconds + 600.0
                if job.source_type == SourceType.url
                else _STAGE_QUIET_BOUNDS["downloading_upload"]
            )
        elif job.status.value == "dispatched":
            bound = (
                settings.node_assignment_max_seconds + 600.0
                if (job.executor or "").startswith("node:")
                else max(
                    settings.runpod_queue_timeout_seconds,
                    settings.runpod_cold_start_seconds,
                )
                + settings.runpod_worker_stall_seconds
                + 600.0
            )
        else:
            bound = _STAGE_QUIET_BOUNDS.get(job.status.value, 3600.0)
        quiet = _last_meaningful_event_age(events)
        if (
            stage_age is not None
            and stage_age > bound
            and quiet is not None
            and quiet > min(bound, 600.0)
        ):
            ev.add(
                job.status.value,
                "stage_progress",
                expected=f"some meaningful event within {bound:.0f}s of stage entry",
                observed=(
                    f"in {job.status.value} for {stage_age:.0f}s, "
                    f"last meaningful event {quiet:.0f}s ago"
                ),
                proof="job_events wall-clock ages",
                status="mismatch",
                flag="stage_stalled",
            )

    # 8. Ready -> track join (the ready state must be backed by a row).
    derived = track_id_for_job(job.id)
    if job.status.value == "ready":
        if track is None or job.track_id is None:
            ev.add(
                "ready",
                "track_row",
                expected=f"ready job links track {derived}",
                observed=(
                    "no track row"
                    if track is None
                    else f"job.track_id is null (track row {track.id} exists)"
                ),
                proof="jobs.track_id -> tracks",
                status="missing",
                flag="ready_without_track",
            )
        elif job.track_id != derived or track.id != derived:
            ev.add(
                "ready",
                "track_row",
                expected=f"ready job links the deterministic track {derived}",
                observed=f"job.track_id={job.track_id}, tracks.id={track.id}",
                proof="jobs.track_id -> tracks",
                status="mismatch",
                flag="ready_without_track",
            )
        else:
            ev.add(
                "ready",
                "track_row",
                expected=f"ready job links track {derived}",
                observed=(
                    f"linked, generation {track.generation}, prefix {track.s3_prefix}"
                    + (" (soft-deleted)" if track.deleted_at else "")
                ),
                proof="jobs.track_id -> tracks",
                status="ok",
            )
    else:
        ev.add(
            "ready",
            "track_row",
            expected="a track row when the job is ready",
            observed="job is not ready",
            proof="jobs.track_id -> tracks",
            status="not_yet",
        )

    return ev


async def _job_db_state(
    request: Request, job: Job
) -> tuple[list[JobEvent], list[NodeAssignment], dict[str, str], Track | None]:
    events = await _jobs(request).list_events(job.id)
    assignments = await _node_repo(request).list_for_job(job.id)
    nodes_list = await _node_repo(request).list_nodes()
    busy_pointers = {
        str(n.busy_assignment_id): n.id
        for n in nodes_list
        if n.busy_assignment_id is not None
    }
    track: Track | None = None
    if job.track_id is not None:
        track = await _tracks(request).get(job.track_id)
    elif job.status.value == "ready":
        track = await _tracks(request).get(track_id_for_job(job.id))
    return events, assignments, busy_pointers, track


# --- S3 evidence (?verify=s3) ---------------------------------------------------


class _HeadOutcome:
    __slots__ = ("head", "error")

    def __init__(self, head: dict[str, Any] | None, error: str | None) -> None:
        self.head = head
        self.error = error


def _head_blocking(settings: Settings, key: str) -> _HeadOutcome:
    try:
        s3 = s3_client(settings)
        head: dict[str, Any] = s3.head_object(
            Bucket=settings.s3_media_bucket, Key=key, ChecksumMode="ENABLED"
        )
        return _HeadOutcome(head, None)
    except Exception as exc:
        from botocore.exceptions import ClientError  # local import: optional dep shape

        if isinstance(exc, ClientError) and exc.response.get("Error", {}).get("Code") in (
            "404",
            "NoSuchKey",
            "NotFound",
        ):
            return _HeadOutcome(None, None)
        return _HeadOutcome(None, str(exc)[:200])


def _stored_sha256_hex(head: dict[str, Any]) -> str | None:
    stored = head.get("ChecksumSHA256")
    if not isinstance(stored, str) or not stored:
        return None
    try:
        return base64.b64decode(stored, validate=True).hex()
    except Exception:
        return None


def _describe_head(head: dict[str, Any] | None) -> str:
    if head is None:
        return "absent"
    size = head.get("ContentLength")
    return f"present ({size} bytes)" if size is not None else "present"


async def _add_s3_evidence(
    ev: _Evidence,
    job: Job,
    events: list[JobEvent],
    track: Track | None,
    settings: Settings,
    stages: set[str] | None = None,
) -> None:
    stages = stages if stages is not None else _stages_reached(job, events)
    track_id = track_id_for_job(job.id)
    prefix = _selected_package_prefix(job, events, stages)

    async def head(key: str) -> _HeadOutcome:
        return await asyncio.to_thread(_head_blocking, settings, key)

    def note(
        stage: str,
        name: str,
        *,
        key: str,
        expected: str,
        applies: bool,
        outcome: _HeadOutcome,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
        missing_is_not_yet: bool = False,
    ) -> None:
        if not applies:
            ev.add(
                stage, name, expected=expected, observed="pipeline has not reached this stage",
                proof=f"S3 HEAD {key}", status="not_yet",
            )
            return
        if outcome.error is not None:
            ev.add(
                stage, name, expected=expected,
                observed=f"S3 check failed: {outcome.error}",
                proof=f"S3 HEAD {key}", status="unknown",
            )
            return
        if outcome.head is None:
            ev.add(
                stage, name, expected=expected, observed="absent",
                proof=f"S3 HEAD {key}",
                status="not_yet" if missing_is_not_yet else "missing",
            )
            return
        observations = [_describe_head(outcome.head)]
        status: EvidenceStatus = "ok"
        actual_size = outcome.head.get("ContentLength")
        if expected_size is not None and actual_size is not None and int(actual_size) != expected_size:
            observations.append(
                f"size {actual_size} != recorded {expected_size}"
            )
            status = "mismatch"
        stored = _stored_sha256_hex(outcome.head)
        if expected_sha256 is not None:
            if stored is None:
                observations.append("no stored checksum to compare")
            elif stored != expected_sha256:
                observations.append(f"sha256 {stored} != recorded {expected_sha256}")
                status = "mismatch"
            else:
                observations.append("sha256 matches")
        ev.add(
            stage, name, expected=expected, observed="; ".join(observations),
            proof=f"S3 HEAD {key}", status=status,
        )

    # Source: expected once the job reached dispatched — the source transfer
    # happens during downloading (upload jobs upload to S3; URL jobs are
    # proven by source.json). A job that FAILED during downloading may never
    # have transferred its source, so it stays not_yet rather than missing.
    source_applies = "dispatched" in stages
    outcome = await head(cloud.source_key(track_id))
    note(
        "downloading",
        "source",
        key=cloud.source_key(track_id),
        expected="sources/{track}/source.mp4 exists (size and checksum match when recorded)",
        applies=source_applies,
        outcome=outcome,
        expected_sha256=job.input_checksum,
    )
    if job.source_type == SourceType.url:
        receipt = await head(node_routing.source_json_key(track_id))
        note(
            "downloading",
            "source_receipt",
            key=node_routing.source_json_key(track_id),
            expected="sources/{track}/source.json (A12: written last) once the download completed",
            applies=source_applies,
            outcome=receipt,
        )

    # Attempt package: dispatch.json (worker claim), handoff.json + six WAVs
    # (A1: handoff written last proves the package).
    if prefix is not None:
        dispatch_key = f"{prefix}/dispatch.json"
        outcome = await head(dispatch_key)
        note(
            "dispatched",
            "dispatch_receipt",
            key=dispatch_key,
            expected="the attempt's dispatch.json claim once the worker started",
            applies=True,
            outcome=outcome,
            missing_is_not_yet=job.status.value == "dispatched",
        )
        package_applies = "verifying" in stages
        handoff_key = f"{prefix}/handoff.json"
        outcome = await head(handoff_key)
        note(
            "dispatched",
            "handoff",
            key=handoff_key,
            expected="handoff.json exists once the package is complete (A1)",
            applies=package_applies,
            outcome=outcome,
        )
        for role in STEM_ROLES:
            wav_key = f"{prefix}/stems/{role}.wav"
            outcome = await head(wav_key)
            note(
                "dispatched",
                f"stem_{role}",
                key=wav_key,
                expected=f"{role}.wav exists in the attempt package",
                applies=package_applies,
                outcome=outcome,
            )
    else:
        ev.add(
            "dispatched",
            "attempt_package",
            expected="the attempt's dispatch.json, handoff.json and six WAVs",
            observed="job has not been dispatched",
            proof="S3 HEAD (skipped)",
            status="not_yet",
        )

    # Active generation: manifest (written last, C2) + seven media objects.
    if track is not None:
        recorded = {}
        publisher = (track.integrity or {}).get("publisher")
        if isinstance(publisher, dict):
            for obj in publisher.get("objects") or []:
                if isinstance(obj, dict) and isinstance(obj.get("file"), str):
                    recorded[obj["file"]] = obj
        generation_objects: list[tuple[str, str, str]] = [
            ("manifest", track.manifest_key, "manifest.json"),
            ("video", f"{track.s3_prefix}/video.mp4", "video.mp4"),
        ]
        for role in STEM_ROLES:
            generation_objects.append(
                (f"browser_stem_{role}", f"{track.s3_prefix}/stems/{role}.m4a", f"stems/{role}.m4a")
            )
        stage = "ready" if job.status.value == "ready" else "publishing"
        for name, key, rel in generation_objects:
            outcome = await head(key)
            rec = recorded.get(rel) or {}
            recorded_sha = rec.get("sha256")
            note(
                stage,
                name,
                key=key,
                expected=f"{rel} exists in the published generation (manifest written last, C2)",
                applies=True,
                outcome=outcome,
                expected_size=rec.get("size_bytes") if isinstance(rec.get("size_bytes"), int) else None,
                expected_sha256=(
                    recorded_sha
                    if isinstance(recorded_sha, str)
                    and len(recorded_sha) == 64
                    and all(c in "0123456789abcdef" for c in recorded_sha)
                    else None
                ),
            )
    else:
        ev.add(
            "ready",
            "generation_objects",
            expected="manifest + video + six stem m4a in the active generation",
            observed="no track row yet",
            proof="S3 HEAD (skipped)",
            status="not_yet",
        )


# --- jobs ----------------------------------------------------------------------


async def _flags_for(request: Request, jobs: list[Job]) -> dict[str, list[str]]:
    flags: dict[str, list[str]] = {}
    for job in jobs:
        events, assignments, busy_pointers, track = await _job_db_state(request, job)
        evidence = _build_db_evidence(
            job, events, assignments, busy_pointers, track, _settings(request)
        )
        flags[job.id.hex] = evidence.flags
    return flags


@router.get("/jobs")
async def admin_list_jobs(
    request: Request,
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    _auth: None = Admin,
) -> AdminJobListResponse:
    stage: JobStage | None = None
    if status is not None:
        try:
            stage = JobStage(status)
        except ValueError:
            raise HTTPException(422, f"unknown status {status!r}") from None
    jobs, total = await _jobs(request).list_jobs(limit=limit, offset=offset, status=stage)
    counts = {k.value: v for k, v in (await _jobs(request).count_jobs_by_status()).items()}
    flags = await _flags_for(request, jobs)
    return AdminJobListResponse(
        jobs=[AdminJobRow.from_job(job, flags.get(job.id.hex, [])) for job in jobs],
        total=total,
        limit=limit,
        offset=offset,
        counts=counts,
    )


def _parse_job_id(job_id: str) -> uuid.UUID:
    try:
        return uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(404, "Job not found") from None


@router.get("/jobs/{job_id}")
async def admin_job_detail(
    request: Request,
    job_id: str,
    verify: str | None = Query(default=None),
    _auth: None = Admin,
) -> AdminJobDetailResponse:
    job = await _jobs(request).get_job(_parse_job_id(job_id))
    if job is None:
        raise HTTPException(404, "Job not found")
    events, assignments, busy_pointers, track = await _job_db_state(request, job)
    settings = _settings(request)
    # Durable transition/failure history decides which stages a (possibly
    # failed) job actually reached — never the terminal status alone.
    stages = _stages_reached(job, events)
    evidence = _build_db_evidence(
        job, events, assignments, busy_pointers, track, settings, stages
    )
    if verify == "s3":
        await _add_s3_evidence(evidence, job, events, track, settings, stages)

    prefix = _selected_package_prefix(job, events, stages)
    return AdminJobDetailResponse(
        job=AdminJobColumns.from_job(job),
        derivedTrackId=str(track_id_for_job(job.id)),
        dispatchAttempt=DispatchAttempt(
            # Durable dispatch history, not the retry counter: later-stage
            # retries increment jobs.attempt without redispatching.
            idempotencyKey=_selected_dispatch_key(job, events),
            attemptPrefix=prefix,
            runpodJobId=job.runpod_job_id,
        ),
        events=[
            AdminEventInfo(
                id=event.id, event=event.event,
                detail=_redact_value(_event_detail(event)) or None,
                createdAt=event.created_at,
            )
            for event in events
        ],
        assignments=[AdminAssignmentInfo.from_assignment(a) for a in assignments],
        track=(
            AdminTrackInfo(
                id=str(track.id),
                title=_redact_value(track.title),
                artist=_redact_value(track.artist),
                durationSeconds=track.duration_seconds,
                s3Prefix=track.s3_prefix,
                generation=track.generation,
                manifestKey=track.manifest_key,
                integrity=_redact_value(track.integrity),
                deletedAt=track.deleted_at,
                createdAt=track.created_at,
            )
            if track is not None
            else None
        ),
        evidence=evidence.items,
    )


# --- nodes ---------------------------------------------------------------------


@router.get("/nodes")
async def admin_list_nodes(request: Request, _auth: None = Admin) -> AdminNodeListResponse:
    settings = _settings(request)
    now = utcnow()
    node_rows = await _node_repo(request).list_nodes()
    infos: list[AdminNodeInfo] = []
    for node in node_rows:
        seen = _aware(node.last_seen_at)
        online = seen is not None and (now - seen).total_seconds() <= settings.node_online_seconds
        current: AdminCurrentAssignment | None = None
        if node.busy_assignment_id is not None:
            assignment = await _node_repo(request).get_assignment(node.busy_assignment_id)
            if assignment is not None:
                current = AdminCurrentAssignment(
                    id=str(assignment.id),
                    jobId=assignment.job_id.hex,
                    kind=assignment.kind.value,
                    status=assignment.status.value,
                    phase=assignment.phase,
                    heartbeatAt=assignment.heartbeat_at,
                    createdAt=assignment.created_at,
                )
        infos.append(
            AdminNodeInfo(
                id=node.id,
                displayName=_redact_value(node.display_name or node.id),
                online=online,
                enabled=node.disabled_at is None,
                capabilities=dict(node.capabilities or {}),
                agentVersion=node.agent_version,
                lastSeenAt=seen,
                busyAssignmentId=(
                    str(node.busy_assignment_id) if node.busy_assignment_id else None
                ),
                currentAssignment=current,
            )
        )
    return AdminNodeListResponse(nodes=infos, onlineSeconds=settings.node_online_seconds)


async def _set_node_enabled(request: Request, node_id: str, enabled: bool) -> dict[str, bool]:
    if not await _node_repo(request).set_disabled(node_id, not enabled):
        raise HTTPException(404, "Node not found")
    return {"ok": True, "enabled": enabled}


@router.post("/nodes/{node_id}/enable")
async def admin_enable_node(
    request: Request, node_id: str, _auth: None = Admin
) -> dict[str, bool]:
    return await _set_node_enabled(request, node_id, True)


@router.post("/nodes/{node_id}/disable")
async def admin_disable_node(
    request: Request, node_id: str, _auth: None = Admin
) -> dict[str, bool]:
    return await _set_node_enabled(request, node_id, False)


# --- runtime settings ------------------------------------------------------------


async def _settings_response(request: Request) -> AdminSettingsResponse:
    settings = _settings(request)
    stored = await _runtime(request).get_all()

    routing_row = stored.get(NODE_ROUTING_KEY)
    routing_value = routing_row.value if routing_row is not None else None
    routing_mode = normalize_routing(routing_value)
    if routing_mode is not None:
        routing = AdminSettingValue(value=routing_mode, source="database", error=None)
    elif routing_row is not None and routing_value is not None:
        # Stored garbage: report it (the orchestrator falls back to env).
        routing = AdminSettingValue(
            value=routing_value,
            source="database",
            error=validate_setting(NODE_ROUTING_KEY, routing_value),
        )
    else:
        routing = AdminSettingValue(value=settings.node_routing, source="environment", error=None)

    limit_row = stored.get(RUNPOD_MAX_CONCURRENT_KEY)
    limit_value = limit_row.value if limit_row is not None else None
    limit_valid, limit = parse_runpod_limit(limit_value)
    if limit_row is not None and limit_valid:
        limit_item = AdminSettingValue(value=limit, source="database", error=None)
    elif limit_row is not None:
        limit_item = AdminSettingValue(
            value=limit_value,
            source="database",
            error=validate_setting(RUNPOD_MAX_CONCURRENT_KEY, limit_value),
        )
    else:
        limit_item = AdminSettingValue(
            value=settings.runpod_max_concurrent, source="environment", error=None
        )

    return AdminSettingsResponse(
        nodeRouting=routing,
        runpodMaxConcurrent=limit_item,
        runpodConfigured=bool(settings.runpod_api_key and settings.runpod_endpoint_id),
        jobsAtRunpod=await _jobs(request).count_runpod_dispatches(),
    )


@router.get("/settings")
async def admin_get_settings(request: Request, _auth: None = Admin) -> AdminSettingsResponse:
    return await _settings_response(request)


@router.put("/settings")
async def admin_put_settings(
    request: Request, body: AdminSettingsUpdate, _auth: None = Admin
) -> AdminSettingsResponse:
    updates = body.model_fields_set
    if not updates:
        raise HTTPException(422, "Provide at least one setting")
    # Validate everything first; nothing is stored when anything is invalid.
    if "nodeRouting" in updates and body.nodeRouting is not None:
        problem = validate_setting(NODE_ROUTING_KEY, body.nodeRouting)
        if problem is not None:
            raise HTTPException(422, problem)
    if "nodeRouting" in updates:
        await _runtime(request).set_value(NODE_ROUTING_KEY, body.nodeRouting)
    if "runpodMaxConcurrent" in updates:
        await _runtime(request).set_value(RUNPOD_MAX_CONCURRENT_KEY, body.runpodMaxConcurrent)
    return await _settings_response(request)


# --- assignment release -----------------------------------------------------------


@router.post("/assignments/{assignment_id}/release")
async def admin_release_assignment(
    request: Request, assignment_id: str, _auth: None = Admin
) -> AdminReleaseResponse:
    try:
        parsed = uuid.UUID(assignment_id)
    except ValueError:
        raise HTTPException(404, "Assignment not found") from None
    assignment = await _node_repo(request).get_assignment(parsed)
    if assignment is None:
        raise HTTPException(404, "Assignment not found")
    job = await _jobs(request).get_job(assignment.job_id)
    if job is None:
        raise HTTPException(409, "The assignment's job no longer exists")
    if job.status not in TERMINAL_STAGES:
        raise HTTPException(
            409,
            "Only an open assignment on a terminal job can be released; "
            "this job is still live (use the node watchdog instead)",
        )
    if assignment.status not in NODE_ASSIGNMENT_OPEN:
        raise HTTPException(409, f"Assignment is already {assignment.status.value}")
    reason = "released by an administrator"
    released = await _node_repo(request).expire(parsed, reason=reason)
    if not released:
        raise HTTPException(409, "Assignment is already closed")
    await _jobs(request).append_event(
        assignment.job_id,
        "admin_assignment_released",
        {
            "assignment_id": str(assignment.id),
            "node_id": assignment.node_id,
            "kind": assignment.kind.value,
            "reason": reason,
        },
    )
    return AdminReleaseResponse(
        ok=True,
        assignmentId=str(assignment.id),
        nodeId=assignment.node_id,
        jobId=assignment.job_id.hex,
    )


__all__ = ["router"]

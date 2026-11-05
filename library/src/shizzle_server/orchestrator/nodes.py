"""Node routing for the orchestrator (docs/WORKER-NODES.md).

Two decisions live here, both made inside the job's lease:

- ``downloading`` for a URL job: hand the download to an online node and wait
  for its ``sources/<track>/source.json`` marker (A12: written last, proves
  ``source.mp4``). The VPS never downloads; if no node is online the job
  waits up to ``node_download_wait_seconds`` and then fails
  ``NO_NODE_AVAILABLE``.
- ``dispatched`` for any job: prefer an online separation node; after
  ``node_separate_wait_seconds`` with none, fall back to RunPod (B17), unless
  routing is ``only``. A node separation uses the same reservation, receipt
  and handoff-last machinery as RunPod — the executor id ``node:<assignment>``
  rides in ``jobs.runpod_job_id`` so recovery and reconciliation are shared.

Reports from nodes are hints; S3 markers are the truth (``source.json`` here,
``handoff.json`` in ``cloud.package_ready``).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlsplit

from ..api.media import s3_client
from ..db.models import (
    NODE_ASSIGNMENT_OPEN,
    JobEvent,
    JobStage,
    NodeAssignment,
    NodeAssignmentKind,
    NodeAssignmentStatus,
    utcnow,
)
from ..db.repository import track_id_for_job
from ..errors import ErrorCode, StageError
from ..metadata import resolve_track_metadata
from . import cloud
from .runpod_client import NotConfiguredRunPodClient

if TYPE_CHECKING:  # runtime import cycle with stages.py
    from .stages import StageContext

logger = logging.getLogger(__name__)

SOURCE_JSON = "source.json"
SOURCE_INTERFACE = "shizzle-source-v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_YOUTUBE_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}

NODE_WAIT_EVENT = "node_wait"
NODE_FAILED_EVENT = "node_failed"
NODE_EXPIRED_EVENT = "node_expired"
NODE_COMPLETED_EVENT = "node_completed"
FALLBACK_RUNPOD_EVENT = "fallback_runpod"


# --- source identity -----------------------------------------------------------


def youtube_video_id(url: str) -> str | None:
    """The single YouTube video id in ``url``, or None for anything else.

    Accepts watch, youtu.be, shorts, live and embed forms; discards playlist
    and tracking parameters. Ported from the Mac fork's ``ingest/urls.py``.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or parts.username or parts.password:
        return None
    host = (parts.hostname or "").lower()
    candidate: str | None = None
    if host in {"youtu.be", "www.youtu.be"}:
        candidate = parts.path.removeprefix("/").split("/")[0]
    elif host in _YOUTUBE_HOSTS and parts.path == "/watch":
        values = parse_qs(parts.query).get("v", [])
        candidate = values[0] if len(values) == 1 else None
    elif host in _YOUTUBE_HOSTS:
        segments = parts.path.split("/")
        if len(segments) == 3 and segments[1] in {"shorts", "live", "embed"}:
            candidate = segments[2]
    if candidate and _VIDEO_ID_RE.fullmatch(candidate):
        return candidate
    return None


def source_ref_for_url(url: str) -> str:
    """Stable, path-safe identity for a URL source: ``youtube-<id>`` or ``sha256-<digest>``."""
    video_id = youtube_video_id(url)
    if video_id is not None:
        return f"youtube-{video_id}"
    return "sha256-" + hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:32]


def source_json_key(track_id: uuid.UUID | str) -> str:
    return f"sources/{track_id}/{SOURCE_JSON}"


# --- small helpers -------------------------------------------------------------


def _age_seconds(since: datetime | None) -> float | None:
    if since is None:
        return None
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return (utcnow() - since).total_seconds()


def _event_age(events: list[JobEvent], name: str, **match: Any) -> float | None:
    """Age of the FIRST event named ``name`` whose detail matches ``match``."""
    for event in events:
        detail = event.detail if isinstance(event.detail, dict) else {}
        if event.event == name and all(detail.get(k) == v for k, v in match.items()):
            return _age_seconds(event.created_at)
    return None


def _last_is(events: list[JobEvent], name: str, **match: Any) -> bool:
    if not events:
        return False
    last = events[-1]
    detail = last.detail if isinstance(last.detail, dict) else {}
    return last.event == name and all(detail.get(k) == v for k, v in match.items())


_WAIT_RESET_EVENTS = frozenset(
    {"node_assigned", NODE_EXPIRED_EVENT, NODE_FAILED_EVENT, "retry_scheduled",
     "stage_completed", FALLBACK_RUNPOD_EVENT, "runpod_dispatch_started"}
)


def _wait_already_noted(events: list[JobEvent], kind: str) -> bool:
    """True when the current outage already has its ``node_wait`` row.

    Walks back from the newest event: a ``node_wait`` of this kind means
    noted; any routing or stage event in between means a new outage.
    Progress heartbeats do not reset it (they are not routing decisions).
    """
    for event in reversed(events):
        detail = event.detail if isinstance(event.detail, dict) else {}
        if event.event == NODE_WAIT_EVENT and detail.get("kind") == kind:
            return True
        if event.event in _WAIT_RESET_EVENTS:
            return False
    return False


async def _note_wait(ctx: StageContext, events: list[JobEvent], kind: str, attempt: int) -> None:
    """Record one ``node_wait`` per outage (deduped like runpod_poll_failed, B6)."""
    if not _wait_already_noted(events, kind):
        await ctx.jobs.append_event(
            ctx.job.id,
            NODE_WAIT_EVENT,
            {"kind": kind, "attempt": attempt},
            worker_id=ctx.worker_id,
        )


def _assignment_is_stale(assignment: NodeAssignment, ctx: StageContext) -> str | None:
    """Why an open assignment should be expired, or None while it is healthy."""
    if assignment.status == NodeAssignmentStatus.assigned:
        age = _age_seconds(assignment.created_at) or 0.0
        if age > ctx.settings.node_ack_seconds:
            return f"not acknowledged within {ctx.settings.node_ack_seconds:.0f}s"
        return None
    age = _age_seconds(assignment.heartbeat_at or assignment.updated_at) or 0.0
    if age > ctx.settings.node_stall_seconds:
        return f"no progress for {age:.0f}s (> {ctx.settings.node_stall_seconds:.0f}s)"
    return None


async def _expire_and_retry(
    ctx: StageContext, assignment: NodeAssignment, reason: str
) -> StageError:
    """Close a silent assignment and build the retryable error that redispatches."""
    await ctx.nodes.expire(assignment.id, reason=reason)
    await ctx.jobs.append_event(
        ctx.job.id,
        NODE_EXPIRED_EVENT,
        {
            "assignment_id": str(assignment.id),
            "node_id": assignment.node_id,
            "kind": assignment.kind.value,
            "reason": reason,
        },
        worker_id=ctx.worker_id,
    )
    return StageError(
        ErrorCode.NODE_FAILED,
        f"node {assignment.node_id} {assignment.kind.value}: {reason}",
        retryable=True,
    )


async def _mirror_phase(ctx: StageContext, assignment: NodeAssignment) -> None:
    phase = assignment.phase or assignment.status.value
    await ctx.jobs.record_worker_progress(
        ctx.job.id, phase=f"{assignment.node_id}: {phase}"[:256], worker_id=ctx.worker_id
    )


# --- downloading: URL -> sources/<track>/source.mp4 via a node ------------------


async def download_via_node(ctx: StageContext) -> JobStage | None:
    """Drive a URL job's download through a node. Returns the next stage or parks."""
    job = ctx.job
    track_id = track_id_for_job(job.id)
    events = await ctx.jobs.list_events(job.id)
    assignment = await ctx.nodes.latest_for_job(job.id, kind=NodeAssignmentKind.download)

    if assignment is not None and assignment.status in NODE_ASSIGNMENT_OPEN:
        reason = _assignment_is_stale(assignment, ctx)
        if reason is not None:
            raise await _expire_and_retry(ctx, assignment, reason)
        await _mirror_phase(ctx, assignment)
        ctx.park_seconds = ctx.settings.node_poll_seconds
        return None

    if assignment is not None and assignment.status == NodeAssignmentStatus.completed:
        receipt = await _read_source_receipt(ctx, track_id)
        if receipt is None:
            # The node says done but the marker is not visible yet. Give it the
            # stall window from its report, then treat the attempt as lost.
            if (_age_seconds(assignment.updated_at) or 0.0) > ctx.settings.node_stall_seconds:
                raise await _expire_and_retry(
                    ctx, assignment, "reported complete but source.json never appeared"
                )
            ctx.park_seconds = ctx.settings.node_poll_seconds
            return None
        problem = await _verify_source_receipt(ctx, track_id, receipt)
        if problem is not None:
            await ctx.jobs.append_event(
                job.id,
                NODE_FAILED_EVENT,
                {"assignment_id": str(assignment.id), "node_id": assignment.node_id,
                 "kind": "download", "reason": problem},
                worker_id=ctx.worker_id,
            )
            raise StageError(
                ErrorCode.CHECKSUM_MISMATCH,
                f"node {assignment.node_id} source receipt rejected: {problem}",
                retryable=True,
            )
        await ctx.jobs.set_input_checksum(
            job.id, worker_id=ctx.worker_id, checksum=str(receipt["sha256"])
        )
        if not job.title or not job.artist:
            # The node saw the video's own title/uploader; clean them the same
            # way the upload path cleans a filename, never over user-typed text.
            resolved = resolve_track_metadata(
                job.title, job.artist, str(receipt.get("title") or "")
            )
            await ctx.jobs.set_metadata(
                job.id,
                worker_id=ctx.worker_id,
                title=resolved.title or None,
                artist=resolved.artist or str(receipt.get("uploader") or ""),
            )
        if not any(e.event == NODE_COMPLETED_EVENT and (e.detail or {}).get("kind") == "download"
                   for e in events):
            await ctx.jobs.append_event(
                job.id,
                NODE_COMPLETED_EVENT,
                {
                    "assignment_id": str(assignment.id),
                    "node_id": assignment.node_id,
                    "kind": "download",
                    "bytes": receipt.get("bytes"),
                    "duration_seconds": receipt.get("duration_seconds"),
                },
                worker_id=ctx.worker_id,
            )
        ctx.detail["source"] = {
            "node_id": assignment.node_id,
            "source_ref": receipt.get("source_ref"),
            "bytes": receipt.get("bytes"),
        }
        return JobStage.dispatched

    if (
        assignment is not None
        and assignment.status == NodeAssignmentStatus.failed
        and (assignment.payload or {}).get("attempt") == job.attempt
    ):
        # First observation of this failure: burn the attempt once. On the next
        # attempt the stale failed row no longer matches and a fresh node is picked.
        detail = assignment.detail if isinstance(assignment.detail, dict) else {}
        code = str(detail.get("code") or ErrorCode.NODE_FAILED.value)
        if not _last_is(events, NODE_FAILED_EVENT, assignment_id=str(assignment.id)):
            await ctx.jobs.append_event(
                job.id,
                NODE_FAILED_EVENT,
                {"assignment_id": str(assignment.id), "node_id": assignment.node_id,
                 "kind": "download", "code": code,
                 "detail": str(detail.get("detail") or "")[:500]},
                worker_id=ctx.worker_id,
            )
        raise StageError(
            ErrorCode.NODE_FAILED,
            f"node {assignment.node_id} download failed: {code} "
            f"{str(detail.get('detail') or '')[:300]}".strip(),
            retryable=True,
        )

    # No usable assignment (none yet, or the last one expired): assign or wait.
    candidates = await ctx.nodes.available(
        NodeAssignmentKind.download, online_seconds=ctx.settings.node_online_seconds
    )
    for node in candidates:
        created = await ctx.nodes.create_assignment(
            job.id,
            worker_id=ctx.worker_id,
            node_id=node.id,
            kind=NodeAssignmentKind.download,
            payload=_download_payload(ctx, track_id),
        )
        if created is not None:
            logger.info("job %s: download assigned to node %s (%s)", job.id, node.id, created.id)
            await ctx.jobs.record_worker_progress(
                job.id, phase=f"{node.id}: assigned", worker_id=ctx.worker_id
            )
            ctx.park_seconds = ctx.settings.node_poll_seconds
            return None

    waited = _event_age(events, NODE_WAIT_EVENT, kind="download")
    if waited is not None and waited > ctx.settings.node_download_wait_seconds:
        raise StageError(
            ErrorCode.NO_NODE_AVAILABLE,
            "no download node came online within "
            f"{ctx.settings.node_download_wait_seconds:.0f}s; start the node agent and resubmit",
            retryable=False,
        )
    await _note_wait(ctx, events, "download", job.attempt)
    await ctx.jobs.record_worker_progress(
        job.id, phase="waiting for a download node", worker_id=ctx.worker_id
    )
    ctx.park_seconds = ctx.settings.node_poll_seconds
    return None


def _download_payload(ctx: StageContext, track_id: uuid.UUID) -> dict[str, Any]:
    url = ctx.job.source_ref
    return {
        "attempt": ctx.job.attempt,
        "url": url,
        "source_ref": source_ref_for_url(url),
        "track_id": str(track_id),
        "bucket": ctx.settings.s3_media_bucket,
        "source_key": cloud.source_key(track_id),
        "source_json_key": source_json_key(track_id),
        "max_duration_seconds": ctx.settings.max_duration_seconds,
        "max_bytes": ctx.settings.max_upload_bytes,
        "title": ctx.job.title,
        "artist": ctx.job.artist,
    }


async def _read_source_receipt(ctx: StageContext, track_id: uuid.UUID) -> dict[str, Any] | None:
    key = source_json_key(track_id)
    try:
        return await asyncio.to_thread(
            cloud._read_json_or_none, s3_client(ctx.settings), ctx.settings.s3_media_bucket, key
        )
    except Exception as exc:
        raise StageError(
            ErrorCode.S3_UPLOAD_FAILED,
            f"source receipt read failed for {key}: {exc}"[:500],
            retryable=True,
        ) from exc


async def _verify_source_receipt(
    ctx: StageContext, track_id: uuid.UUID, receipt: dict[str, Any]
) -> str | None:
    """A12: the receipt must describe exactly the object the pipeline will use."""
    expected_key = cloud.source_key(track_id)
    if receipt.get("interface") != SOURCE_INTERFACE:
        return f"interface must be {SOURCE_INTERFACE}"
    if receipt.get("object_key") != expected_key:
        return f"object_key must be {expected_key}"
    if str(receipt.get("track_id")) != str(track_id):
        return "track_id mismatch"
    sha = receipt.get("sha256")
    if not isinstance(sha, str) or _SHA256_RE.fullmatch(sha) is None:
        return "sha256 must be a lowercase 64-hex digest"
    size = receipt.get("bytes")
    if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
        return "bytes must be a positive integer"
    if size > ctx.settings.max_upload_bytes:
        return f"source is {size} bytes, above the {ctx.settings.max_upload_bytes} byte limit"
    duration = receipt.get("duration_seconds")
    if isinstance(duration, int | float) and duration > ctx.settings.max_duration_seconds:
        return (
            f"source is {duration:.0f}s, above the "
            f"{ctx.settings.max_duration_seconds}s limit"
        )
    try:
        head = await asyncio.to_thread(
            cloud._head_or_none, s3_client(ctx.settings), ctx.settings.s3_media_bucket, expected_key
        )
    except Exception as exc:
        raise StageError(
            ErrorCode.S3_UPLOAD_FAILED,
            f"source head failed for {expected_key}: {exc}"[:500],
            retryable=True,
        ) from exc
    if head is None:
        return "source.mp4 is not in S3"
    if int(head.get("ContentLength", -1)) != size:
        return f"source.mp4 is {head.get('ContentLength')} bytes, receipt says {size}"
    return None


# --- dispatched: separation on a node, RunPod as fallback ---------------------


async def poll_node_separation(ctx: StageContext, *, dispatch_key: str | None) -> JobStage | None:
    """Reconcile a separation that was handed to a node (executor id in runpod_job_id)."""
    job = ctx.job
    assignment_id = _assignment_id_from_executor(job.runpod_job_id)
    assignment = await ctx.nodes.get_assignment(assignment_id) if assignment_id else None
    if assignment is None:
        await ctx.jobs.record_worker_progress(job.id, phase="failed", worker_id=ctx.worker_id)
        raise StageError(
            ErrorCode.NODE_FAILED,
            f"node assignment {job.runpod_job_id} is missing; redispatching",
            retryable=True,
        )
    track_id = track_id_for_job(job.id)

    if assignment.status in NODE_ASSIGNMENT_OPEN:
        reason = _assignment_is_stale(assignment, ctx)
        if reason is not None:
            await ctx.jobs.record_worker_progress(job.id, phase="failed", worker_id=ctx.worker_id)
            raise await _expire_and_retry(ctx, assignment, reason)
        await _mirror_phase(ctx, assignment)
        ctx.park_seconds = ctx.settings.node_poll_seconds
        return None

    if assignment.status == NodeAssignmentStatus.completed:
        if await cloud.package_ready(ctx, idempotency_key=dispatch_key):
            events = await ctx.jobs.list_events(job.id)
            if not any(event.event == "worker_completed" for event in events):
                await ctx.jobs.append_event(
                    job.id,
                    "worker_completed",
                    detail={"executor": job.executor, "assignment_id": str(assignment.id),
                            **(assignment.detail or {})},
                    worker_id=ctx.worker_id,
                )
            if dispatch_key is not None:
                ctx.detail["package_prefix"] = cloud.attempt_prefix(track_id, dispatch_key)
            return JobStage.verifying
        if (_age_seconds(assignment.updated_at) or 0.0) > ctx.settings.node_stall_seconds:
            await ctx.jobs.record_worker_progress(job.id, phase="failed", worker_id=ctx.worker_id)
            raise await _expire_and_retry(
                ctx, assignment, "reported complete but handoff.json never appeared"
            )
        ctx.park_seconds = ctx.settings.node_poll_seconds
        return None

    # failed or expired
    detail = assignment.detail if isinstance(assignment.detail, dict) else {}
    await ctx.jobs.record_worker_progress(job.id, phase="failed", worker_id=ctx.worker_id)
    events = await ctx.jobs.list_events(job.id)
    if not _last_is(events, NODE_FAILED_EVENT, assignment_id=str(assignment.id)):
        await ctx.jobs.append_event(
            job.id,
            NODE_FAILED_EVENT,
            {"assignment_id": str(assignment.id), "node_id": assignment.node_id,
             "kind": "separate", "status": assignment.status.value,
             "code": detail.get("code"), "detail": str(detail.get("detail") or "")[:500]},
            worker_id=ctx.worker_id,
        )
    raise StageError(
        ErrorCode.NODE_FAILED,
        f"node {assignment.node_id} separation {assignment.status.value}: "
        f"{detail.get('code') or ''} {str(detail.get('detail') or '')[:300]}".strip(),
        retryable=True,
    )


def _assignment_id_from_executor(value: str | None) -> uuid.UUID | None:
    if not value or not value.startswith("node:"):
        return None
    try:
        return uuid.UUID(value[5:])
    except ValueError:
        return None


def executor_id(assignment_id: uuid.UUID) -> str:
    """The value a node separation stores in ``jobs.runpod_job_id`` and the receipt."""
    return f"node:{assignment_id}"


def is_node_executor(value: str | None) -> bool:
    return value is not None and value.startswith("node:")


async def choose_separation_node(ctx: StageContext, *, dispatch_key: str) -> str | None:
    """Routing decision for a fresh dispatch.

    Returns a node id to assign, ``"runpod"`` to use the cloud, or None to
    keep parking (the caller parks). Emits ``node_wait`` / ``fallback_runpod``
    events exactly once per wait.
    """
    settings = ctx.settings
    if not settings.node_routing_enabled:
        return "runpod"
    candidates = await ctx.nodes.available(
        NodeAssignmentKind.separate, online_seconds=settings.node_online_seconds
    )
    if candidates:
        return str(candidates[0].id)
    runpod_configured = not isinstance(ctx.runpod, NotConfiguredRunPodClient)
    if settings.node_routing == "prefer" and runpod_configured:
        # No node has ever registered: nothing to wait for, RunPod right away.
        # (A registered-but-offline node earns the wait window: it may wake.)
        registered = await ctx.nodes.list_nodes()
        if not any(
            isinstance(n.capabilities, dict) and n.capabilities.get("separate")
            for n in registered
        ):
            return "runpod"
    events = await ctx.jobs.list_events(ctx.job.id)
    if settings.node_routing == "only" or not runpod_configured:
        await _note_wait(ctx, events, "separate", ctx.job.attempt)
        await ctx.jobs.record_worker_progress(
            ctx.job.id, phase="waiting for a separation node", worker_id=ctx.worker_id
        )
        return None
    waited = _event_age(events, NODE_WAIT_EVENT, kind="separate", attempt=ctx.job.attempt)
    if waited is None:
        await _note_wait(ctx, events, "separate", ctx.job.attempt)
        await ctx.jobs.record_worker_progress(
            ctx.job.id, phase="waiting for a separation node", worker_id=ctx.worker_id
        )
        return None
    if waited <= settings.node_separate_wait_seconds:
        return None
    if not any(
        e.event == FALLBACK_RUNPOD_EVENT and (e.detail or {}).get("idempotency_key") == dispatch_key
        for e in events
    ):
        await ctx.jobs.append_event(
            ctx.job.id,
            FALLBACK_RUNPOD_EVENT,
            {"idempotency_key": dispatch_key, "waited_seconds": round(waited, 1)},
            worker_id=ctx.worker_id,
        )
    return "runpod"


async def assign_separation(
    ctx: StageContext, *, node_id: str, dispatch_key: str, payload: dict[str, Any]
) -> bool:
    """Hand a separation to a node, then reserve and confirm the dispatch.

    Order matters for crash safety: the assignment (locked on the node row)
    is created first, so a crash before the reservation leaves an assignment
    the next pass adopts under the same key (see ``adopt_existing_separation``)
    instead of an orphan. The node derives its executor id as
    ``node:<assignment id>`` and writes it into the S3 receipt, so
    ``cloud.accepted_dispatch_id`` reconciles it exactly like a RunPod id.
    Returns False when the node was taken in between (caller parks).
    """
    assignment = await ctx.nodes.create_assignment(
        ctx.job.id,
        worker_id=ctx.worker_id,
        node_id=node_id,
        kind=NodeAssignmentKind.separate,
        payload=payload,
        idempotency_key=dispatch_key,
    )
    if assignment is None:
        return False
    exec_id = executor_id(assignment.id)
    await ctx.jobs.reserve_dispatch(
        ctx.job.id, worker_id=ctx.worker_id, idempotency_key=dispatch_key
    )
    await ctx.jobs.confirm_dispatch(
        ctx.job.id, idempotency_key=dispatch_key, runpod_job_id=exec_id
    )
    await ctx.jobs.record_worker_progress(
        ctx.job.id, phase=f"{node_id}: assigned", worker_id=ctx.worker_id
    )
    logger.info("job %s: separation assigned to node %s (%s)", ctx.job.id, node_id, exec_id)
    return True


async def adopt_existing_separation(ctx: StageContext, *, dispatch_key: str) -> bool:
    """Crash recovery: an assignment created under this key but never confirmed.

    Returns True after confirming it (the caller parks and polls it next).
    """
    existing = await ctx.nodes.latest_for_job(ctx.job.id, kind=NodeAssignmentKind.separate)
    if (
        existing is None
        or existing.idempotency_key != dispatch_key
        or existing.status in (NodeAssignmentStatus.failed, NodeAssignmentStatus.expired)
    ):
        return False
    await ctx.jobs.reserve_dispatch(
        ctx.job.id, worker_id=ctx.worker_id, idempotency_key=dispatch_key
    )
    await ctx.jobs.confirm_dispatch(
        ctx.job.id, idempotency_key=dispatch_key, runpod_job_id=executor_id(existing.id)
    )
    await ctx.jobs.append_event(
        ctx.job.id,
        "node_assignment_adopted",
        {"assignment_id": str(existing.id), "node_id": existing.node_id,
         "idempotency_key": dispatch_key},
        worker_id=ctx.worker_id,
    )
    return True


__all__ = [
    "adopt_existing_separation",
    "assign_separation",
    "choose_separation_node",
    "download_via_node",
    "executor_id",
    "is_node_executor",
    "poll_node_separation",
    "source_json_key",
    "source_ref_for_url",
    "youtube_video_id",
]


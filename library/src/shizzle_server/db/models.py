"""SQLAlchemy 2.x models: jobs, append-only job_events, tracks, orchestrator heartbeat.

Types are chosen to work on both Postgres (production, JSONB) and SQLite
(local single-container profile): portable sa.Enum(native_enum=False),
sa.Uuid, and JSON with a JSONB variant.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONType = JSON().with_variant(JSONB(), "postgresql")


def utcnow() -> datetime:
    return datetime.now(UTC)


class SourceType(StrEnum):
    url = "url"
    upload = "upload"


class JobStage(StrEnum):
    """Design spec section 4: pending → downloading → dispatched → splitting →
    verifying → publishing → ready | failed."""

    pending = "pending"
    downloading = "downloading"
    dispatched = "dispatched"
    splitting = "splitting"
    verifying = "verifying"
    publishing = "publishing"
    ready = "ready"
    failed = "failed"


TERMINAL_STAGES = frozenset({JobStage.ready, JobStage.failed})

# Stages the orchestrator loop claims and executes. `dispatched` is owned by
# the claim-based RunPod reconciler (one pass per claim, parks between polls);
# the webhook receiver is out of scope this phase — polling reconciliation
# owns dispatched end to end.
RUNNABLE_STAGES = (
    JobStage.pending,
    JobStage.downloading,
    JobStage.dispatched,
    JobStage.splitting,
    JobStage.verifying,
    JobStage.publishing,
)

# Legal forward transitions (state-machine authority, enforced by the repository).
# In cloud mode `dispatched` IS the splitting (decision 6: no splitting-noop
# hop) — it advances straight to `verifying` once the worker package is proven.
ALLOWED_TRANSITIONS: dict[JobStage, frozenset[JobStage]] = {
    JobStage.pending: frozenset({JobStage.downloading, JobStage.failed}),
    JobStage.downloading: frozenset({JobStage.dispatched, JobStage.splitting, JobStage.failed}),
    JobStage.dispatched: frozenset({JobStage.splitting, JobStage.verifying, JobStage.failed}),
    JobStage.splitting: frozenset({JobStage.verifying, JobStage.failed}),
    JobStage.verifying: frozenset({JobStage.publishing, JobStage.failed}),
    JobStage.publishing: frozenset({JobStage.ready, JobStage.failed}),
    JobStage.ready: frozenset(),
    JobStage.failed: frozenset(),
}


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    source_type: Mapped[SourceType] = mapped_column(
        Enum(SourceType, native_enum=False, length=16), nullable=False
    )
    # URL for url-sourced jobs; relative path of the uploaded file for uploads.
    source_ref: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    # Clean performer name resolved at ingest (never folded into the title).
    artist: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[JobStage] = mapped_column(
        Enum(JobStage, native_enum=False, length=16),
        nullable=False,
        default=JobStage.pending,
        index=True,
    )
    runpod_job_id: Mapped[str | None] = mapped_column(String(128))
    # Latest worker progress heartbeat; phase changes are also kept in job_events.
    worker_phase: Mapped[str | None] = mapped_column(String(256))
    worker_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    profile_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_checksum: Mapped[str | None] = mapped_column(String(128))
    output_checksums: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail: Mapped[str | None] = mapped_column(Text)
    stage_timings: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    # Linkage written by the publishing stage (idempotent re-runs check it).
    track_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tracks.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )


class JobEvent(Base):
    """Append-only history. Never updated, never deleted."""

    __tablename__ = "job_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    artist: Mapped[str] = mapped_column(Text, nullable=False, default="")
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # Local Phase 2 placeholder: "local/{job_dir}". Phase 3 publisher writes
    # the real immutable prefix "tracks/{track_id}/{generation}".
    s3_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    manifest_key: Mapped[str] = mapped_column(Text, nullable=False)
    integrity: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TrackGenerationEvent(Base):
    """Append-only activation and rollback ledger for immutable media generations."""

    __tablename__ = "track_generation_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    track_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event: Mapped[str] = mapped_column(String(32), nullable=False)
    from_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    to_generation: Mapped[int] = mapped_column(Integer, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class PlaybackSession(Base):
    """One browser/track/generation observation window."""

    __tablename__ = "playback_sessions"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    track_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    app_build: Mapped[str] = mapped_column(String(128), nullable=False, default="unknown")
    browser: Mapped[str] = mapped_column(String(256), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="started")
    last_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        onupdate=utcnow,
        server_default=func.now(),
    )
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PlaybackEvent(Base):
    """Append-only, idempotent first-party playback incident/evidence event."""

    __tablename__ = "playback_events"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence", name="uq_playback_events_session_sequence"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("playback_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    track_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tracks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    client_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )


class OrchestratorHeartbeat(Base):
    """Liveness row per orchestrator worker, surfaced by /api/health."""

    __tablename__ = "orchestrator_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )

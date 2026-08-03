"""Phase 2 initial schema: jobs, job_events (append-only), tracks, orchestrator_heartbeats.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-02

Self-contained on purpose (no imports from shizzle_server) so future model
drift never rewrites history. Types mirror db/models.py: portable
non-native enums (VARCHAR + CHECK), sa.Uuid, JSON with a JSONB variant.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "tracks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("artist", sa.Text(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("s3_prefix", sa.Text(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("manifest_key", sa.Text(), nullable=False),
        sa.Column("integrity", JSONType, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "source_type",
            sa.Enum("url", "upload", name="sourcetype", native_enum=False, length=16),
            nullable=False,
        ),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "downloading",
                "dispatched",
                "splitting",
                "verifying",
                "publishing",
                "ready",
                "failed",
                name="jobstage",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("runpod_job_id", sa.String(length=128), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=128), nullable=False),
        sa.Column("lease_owner", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("input_checksum", sa.String(length=128), nullable=True),
        sa.Column("output_checksums", JSONType, nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("stage_timings", JSONType, nullable=True),
        sa.Column("track_id", sa.Uuid(), sa.ForeignKey("tracks.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_jobs_idempotency_key"),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_created_at", "jobs", ["created_at"])

    op.create_table(
        "job_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event", sa.String(length=64), nullable=False),
        sa.Column("detail", JSONType, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_job_events_job_id", "job_events", ["job_id"])

    op.create_table(
        "orchestrator_heartbeats",
        sa.Column("worker_id", sa.String(length=128), primary_key=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("orchestrator_heartbeats")
    op.drop_index("ix_job_events_job_id", table_name="job_events")
    op.drop_table("job_events")
    op.drop_index("ix_jobs_created_at", table_name="jobs")
    op.drop_index("ix_jobs_status", table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("tracks")

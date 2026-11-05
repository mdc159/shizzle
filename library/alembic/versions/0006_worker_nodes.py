"""Worker nodes: nodes, node_assignments, jobs.executor (docs/WORKER-NODES.md).

Revision ID: 0006_worker_nodes
Revises: 0005_job_artist
Create Date: 2026-11-05

Self-contained on purpose (no imports from shizzle_server), like 0001.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_worker_nodes"
down_revision = "0005_job_artist"
branch_labels = None
depends_on = None

JSONType = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column("jobs", sa.Column("executor", sa.String(length=64), nullable=True))

    op.create_table(
        "nodes",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("capabilities", JSONType, nullable=True),
        sa.Column("agent_version", sa.String(length=64), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("busy_assignment_id", sa.Uuid(), nullable=True),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "node_assignments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "job_id", sa.Uuid(), sa.ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "node_id",
            sa.String(length=64),
            sa.ForeignKey("nodes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            sa.Enum(
                "download", "separate", name="nodeassignmentkind", native_enum=False, length=16
            ),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum(
                "assigned",
                "acknowledged",
                "running",
                "completed",
                "failed",
                "expired",
                name="nodeassignmentstatus",
                native_enum=False,
                length=16,
            ),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("payload", JSONType, nullable=True),
        sa.Column("phase", sa.String(length=256), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("detail", JSONType, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_node_assignments_job_id", "node_assignments", ["job_id"])
    op.create_index("ix_node_assignments_node_id", "node_assignments", ["node_id"])
    op.create_index("ix_node_assignments_status", "node_assignments", ["status"])


def downgrade() -> None:
    op.drop_index("ix_node_assignments_status", table_name="node_assignments")
    op.drop_index("ix_node_assignments_node_id", table_name="node_assignments")
    op.drop_index("ix_node_assignments_job_id", table_name="node_assignments")
    op.drop_table("node_assignments")
    op.drop_table("nodes")
    op.drop_column("jobs", "executor")

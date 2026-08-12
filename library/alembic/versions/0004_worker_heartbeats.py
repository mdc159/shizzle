"""Record RunPod worker phase heartbeats on jobs.

Revision ID: 0004_worker_heartbeats
Revises: 0003_playback_event_bigint
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0004_worker_heartbeats"
down_revision = "0003_playback_event_bigint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("worker_phase", sa.String(length=256), nullable=True))
    op.add_column(
        "jobs", sa.Column("worker_heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("jobs", "worker_heartbeat_at")
    op.drop_column("jobs", "worker_phase")

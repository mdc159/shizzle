"""Dismissed jobs and separation-only node disablement.

Revision ID: 0008_job_dismissal
Revises: 0007_runtime_settings
Create Date: 2026-11-17
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_job_dismissal"
down_revision = "0007_runtime_settings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "nodes",
        sa.Column("separation_disabled_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("nodes", "separation_disabled_at")
    op.drop_column("jobs", "dismissed_at")

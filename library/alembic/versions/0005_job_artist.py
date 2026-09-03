"""Jobs carry a clean artist resolved at ingest.

Revision ID: 0005_job_artist
Revises: 0004_worker_heartbeats
Create Date: 2026-09-03
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0005_job_artist"
down_revision = "0004_worker_heartbeats"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # server_default keeps every existing row valid; the app default takes
    # over for new rows.
    op.add_column(
        "jobs",
        sa.Column("artist", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("jobs", "artist")
